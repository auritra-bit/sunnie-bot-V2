import os
import base64
import re
import time
import json
import gspread
import requests
import threading
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.background import BackgroundScheduler
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2 import service_account

# ========== Decode YouTube Token from ENV ==========
if os.getenv('YOUTUBE_TOKEN_BASE64'):
    with open('youtube_token.json', 'w') as f:
        f.write(base64.b64decode(os.getenv('YOUTUBE_TOKEN_BASE64')).decode())

app = Flask(__name__)

# ========== Configuration ==========
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_CREDS_JSON", "credentials.json")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "client_secret.json")
SPREADSHEET_NAME = "StudyPlusData"
HF_API_URL = "https://api-inference.huggingface.co/models/microsoft/DialoGPT-medium"
HF_TOKEN = os.getenv("HF_TOKEN", "your_hf_token")
YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID", "your_youtube_channel_id")

# ========== Google Sheets Setup ==========
def init_google_sheets():
    scope = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    
    # Handle environment variable for Render deployment
    if os.getenv('GOOGLE_CREDS_JSON'):
        creds_json = json.loads(os.getenv('GOOGLE_CREDS_JSON'))
        creds = service_account.Credentials.from_service_account_info(creds_json, scopes=scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    
    client = gspread.authorize(creds)
    
    try:
        workbook = client.open(SPREADSHEET_NAME)
    except gspread.SpreadsheetNotFound:
        workbook = client.create(SPREADSHEET_NAME)
        workbook.share(None, perm_type='anyone', role='writer')
    
    # Define sheets and their headers
    sheets_def = {
        "Users": ["UserID", "Username", "TotalXP", "CurrentStreak", "TotalStudyMinutes", "Rank", "JoinDate", "LastActive", "Status", "Badges"],
        "Sessions": ["SessionID", "UserID", "Username", "StartTime", "LastActivity", "Status", "BreakEndTime", "TotalBreakTime"],
        "Activities": ["ActivityID", "UserID", "Username", "Action", "XPEarned", "Duration", "Description", "Timestamp", "Month"],
        "Tasks": ["TaskID", "UserID", "Username", "TaskName", "Status", "CreatedDate", "CompletedDate", "XPEarned"],
        "Goals": ["GoalID", "UserID", "Username", "Goal", "CreatedDate", "Status"],
        "Reminders": ["ReminderID", "UserID", "Username", "Message", "ReminderTime", "Status", "Type"],
        "Reports": ["ReportID", "UserID", "Username", "ReportedUser", "Reason", "Timestamp", "Status"],
        "Plans": ["PlanID", "UserID", "Username", "PlanRequest", "PlanResponse", "CreatedDate"]
    }
    
    # Create worksheets with headers if not exists
    for sheet_name, headers in sheets_def.items():
        try:
            sheet = workbook.worksheet(sheet_name)
            # Check if headers match
            existing_headers = sheet.row_values(1)
            if existing_headers != headers:
                sheet.clear()
                sheet.append_row(headers)
        except gspread.WorksheetNotFound:
            sheet = workbook.add_worksheet(title=sheet_name, rows=100, cols=len(headers))
            sheet.append_row(headers)
    
    return workbook

workbook = init_google_sheets()
sheets = {sheet.title: sheet for sheet in workbook.worksheets()}

# ========== YouTube API Setup ==========
def get_youtube_service():
    creds = None
    token_file = 'youtube_token.json'
    scopes = ['https://www.googleapis.com/auth/youtube.force-ssl']
    
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_secret = os.getenv('YOUTUBE_CLIENT_SECRET_JSON')
            if client_secret:
                flow = InstalledAppFlow.from_client_config(
                    json.loads(client_secret),
                    scopes
                )
            elif os.path.exists(YOUTUBE_CLIENT_SECRET):
                flow = InstalledAppFlow.from_client_secrets_file(
                    YOUTUBE_CLIENT_SECRET,
                    scopes
                )
            else:
                return None
                
            creds = flow.run_local_server(port=0)
            with open(token_file, 'w') as token:
                token.write(creds.to_json())
    
    return build('youtube', 'v3', credentials=creds)

# Initialize YouTube service
youtube_service = None
if os.path.exists(YOUTUBE_CLIENT_SECRET) or os.getenv('YOUTUBE_CLIENT_SECRET_JSON'):
    try:
        youtube_service = get_youtube_service()
    except Exception as e:
        print(f"YouTube API init failed: {str(e)}")

# ========== Core Bot Functionality ==========
class StudyBot:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.scheduler.start()
        self.sheets_lock = threading.Lock()
        self.live_chat_id = None
        self.last_chat_id = None
        self.setup_scheduled_jobs()
        self.last_activity_check = datetime.now()
    
    def setup_scheduled_jobs(self):
        self.scheduler.add_job(self.check_session_activity, 'interval', minutes=5)
        self.scheduler.add_job(self.cleanup_old_sessions, 'interval', hours=1)
        self.scheduler.add_job(self.cleanup_old_reminders, 'interval', hours=12)
        self.scheduler.add_job(self.send_scheduled_reminders, 'interval', minutes=1)
        self.scheduler.add_job(self.check_live_stream, 'interval', minutes=2)
    
    # ===== Helper Methods =====
    def get_ai_response(self, prompt, max_chars=200):
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}
        payload = {"inputs": prompt}
        
        try:
            response = requests.post(HF_API_URL, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                result = response.json()
                if isinstance(result, list) and len(result) > 0:
                    ai_text = result[0].get('generated_text', '')
                    ai_text = ai_text.replace(prompt, '').strip()
                    return ai_text[:max_chars] + "..." if len(ai_text) > max_chars else ai_text
            return "🤖 AI is busy. Try again later!"
        except:
            return "🤖 AI service unavailable."
    
    def get_rank(self, xp):
        xp = int(xp)
        ranks = [
            (1000, "👑 Legend"),
            (500, "📘 Scholar"),
            (300, "📗 Master"),
            (150, "📙 Intermediate"),
            (50, "📕 Beginner"),
            (0, "🍼 Newbie")
        ]
        for threshold, name in ranks:
            if xp >= threshold:
                return name
        return "🍼 Newbie"
    
    def get_badges(self, total_minutes):
        badges = []
        badge_levels = [
            (500, "💎 Master Scholar"),
            (240, "🔷 Diamond Crown"),
            (150, "🥇 Golden Genius"),
            (110, "🥈 Silver Brain"),
            (50, "🥉 Bronze Mind")
        ]
        for threshold, badge in badge_levels:
            if total_minutes >= threshold:
                badges.append(badge)
        return badges
    
    def get_user_badges(self, total_minutes):
        return ", ".join(self.get_badges(total_minutes))
    
    # ===== User Management =====
    def get_or_create_user(self, userid, username):
        with self.sheets_lock:
            users_sheet = sheets["Users"]
            users = users_sheet.get_all_records()
            
            for i, user in enumerate(users):
                if str(user['UserID']) == str(userid):
                    return i + 2, user
            
            # Create new user
            new_user = {
                "UserID": userid,
                "Username": username,
                "TotalXP": 0,
                "CurrentStreak": 0,
                "TotalStudyMinutes": 0,
                "Rank": "🍼 Newbie",
                "JoinDate": datetime.now().strftime("%Y-%m-%d"),
                "LastActive": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Status": "Inactive",
                "Badges": ""
            }
            users_sheet.append_row(list(new_user.values()))
            return len(users) + 2, new_user
    
    def update_user_xp(self, userid, xp_to_add):
        row_idx, user = self.get_or_create_user(userid, "")
        if not row_idx:
            return
        
        with self.sheets_lock:
            users_sheet = sheets["Users"]
            new_xp = int(user.get('TotalXP', 0)) + xp_to_add
            new_rank = self.get_rank(new_xp)
            
            # Update cells
            users_sheet.update_cell(row_idx, users_sheet.find("TotalXP").col, new_xp)
            users_sheet.update_cell(row_idx, users_sheet.find("Rank").col, new_rank)
            users_sheet.update_cell(row_idx, users_sheet.find("LastActive").col, 
                                  datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            
            # Update badges if needed
            total_minutes = int(user.get('TotalStudyMinutes', 0))
            badges = self.get_user_badges(total_minutes)
            if badges != user.get('Badges', ''):
                users_sheet.update_cell(row_idx, users_sheet.find("Badges").col, badges)
    
    def update_user_study_minutes(self, userid, minutes_to_add):
        row_idx, user = self.get_or_create_user(userid, "")
        if not row_idx:
            return
        
        with self.sheets_lock:
            users_sheet = sheets["Users"]
            new_minutes = int(user.get('TotalStudyMinutes', 0)) + minutes_to_add
            users_sheet.update_cell(row_idx, users_sheet.find("TotalStudyMinutes").col, new_minutes)
            
            # Update badges
            badges = self.get_user_badges(new_minutes)
            users_sheet.update_cell(row_idx, users_sheet.find("Badges").col, badges)
    
    def calculate_streak(self, userid):
        with self.sheets_lock:
            activities_sheet = sheets["Activities"]
            activities = activities_sheet.get_all_records()
            
            attendance_dates = []
            for act in activities:
                if str(act['UserID']) == str(userid) and act['Action'] == 'Attendance':
                    try:
                        date = datetime.strptime(act['Timestamp'], "%Y-%m-%d %H:%M:%S").date()
                        attendance_dates.append(date)
                    except:
                        continue
            
            if not attendance_dates:
                return 0
            
            today = datetime.now().date()
            streak = 0
            current_date = today
            
            while current_date in attendance_dates:
                streak += 1
                current_date -= timedelta(days=1)
            
            return streak
    
    # ===== Session Management =====
    def start_session(self, username, userid, args):
        with self.sheets_lock:
            sessions_sheet = sheets["Sessions"]
            sessions = sessions_sheet.get_all_records()
            
            # Check for existing session
            for session in sessions:
                if str(session['UserID']) == str(userid) and session['Status'] in ['Active', 'Break']:
                    return f"⚠️ {username}, you already have an active session!"
            
            # Create new session
            session_id = str(uuid.uuid4())
            new_session = {
                "SessionID": session_id,
                "UserID": userid,
                "Username": username,
                "StartTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "LastActivity": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Status": "Active",
                "BreakEndTime": "",
                "TotalBreakTime": 0
            }
            sessions_sheet.append_row(list(new_session.values()))
            return f"⏱️ {username}, study session started! Use !stop to end."
    
    def stop_session(self, username, userid, args):
        with self.sheets_lock:
            sessions_sheet = sheets["Sessions"]
            sessions = sessions_sheet.get_all_records()
            activities_sheet = sheets["Activities"]
            
            for i, session in enumerate(sessions):
                if str(session['UserID']) == str(userid) and session['Status'] in ['Active', 'Break']:
                    # Calculate session duration
                    start_time = datetime.strptime(session['StartTime'], "%Y-%m-%d %H:%M:%S")
                    end_time = datetime.now()
                    total_minutes = int((end_time - start_time).total_seconds() / 60)
                    
                    # Subtract break time
                    break_time = int(session.get('TotalBreakTime', 0))
                    study_minutes = max(0, total_minutes - break_time)
                    xp_earned = study_minutes * 2
                    
                    # Log activity
                    activity_id = str(uuid.uuid4())
                    activities_sheet.append_row([
                        activity_id,
                        userid,
                        username,
                        "StudySession",
                        xp_earned,
                        f"{study_minutes} min",
                        f"Studied for {study_minutes} minutes",
                        end_time.strftime("%Y-%m-%d %H:%M:%S"),
                        end_time.strftime("%Y-%m")
                    ])
                    
                    # Update user
                    self.update_user_xp(userid, xp_earned)
                    self.update_user_study_minutes(userid, study_minutes)
                    
                    # Remove session
                    sessions_sheet.delete_rows(i + 2)
                    
                    # Badge message
                    badges = self.get_badges(study_minutes)
                    badge_msg = f" 🎖️ New badge: {badges[-1]}!" if badges else ""
                    
                    return f"🎓 {username}, studied {study_minutes}min, earned {xp_earned}XP!{badge_msg}"
            
            return f"⚠️ {username}, no active session found."
    
    def confirm_working(self, username, userid, args):
        with self.sheets_lock:
            sessions_sheet = sheets["Sessions"]
            sessions = sessions_sheet.get_all_records()
            
            for i, session in enumerate(sessions):
                if str(session['UserID']) == str(userid) and session['Status'] in ['Active', 'Break']:
                    sessions_sheet.update_cell(i + 2, sessions_sheet.find("LastActivity").col, 
                                             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    sessions_sheet.update_cell(i + 2, sessions_sheet.find("Status").col, "Active")
                    return f"✅ {username}, activity confirmed! Keep studying!"
            
            return f"⚠️ {username}, no active session found."
    
    def start_break(self, username, userid, args):
        # Parse break duration (default 20 minutes)
        duration = 20
        if args:
            try:
                duration = int(re.search(r'(\d+)', args).group(1))
                duration = min(duration, 120)  # Max 2 hours
            except:
                pass
        
        with self.sheets_lock:
            sessions_sheet = sheets["Sessions"]
            sessions = sessions_sheet.get_all_records()
            reminders_sheet = sheets["Reminders"]
            
            for i, session in enumerate(sessions):
                if str(session['UserID']) == str(userid) and session['Status'] == 'Active':
                    # Set break end time
                    break_end = datetime.now() + timedelta(minutes=duration)
                    sessions_sheet.update_cell(i + 2, sessions_sheet.find("Status").col, "Break")
                    sessions_sheet.update_cell(i + 2, sessions_sheet.find("BreakEndTime").col, 
                                             break_end.strftime("%Y-%m-%d %H:%M:%S"))
                    
                    # Update total break time
                    current_break = int(session.get('TotalBreakTime', 0))
                    sessions_sheet.update_cell(i + 2, sessions_sheet.find("TotalBreakTime").col, current_break + duration)
                    
                    # Schedule reminder
                    reminder_id = str(uuid.uuid4())
                    reminders_sheet.append_row([
                        reminder_id,
                        userid,
                        username,
                        f"Break time over! Back to studying 📚",
                        break_end.strftime("%Y-%m-%d %H:%M:%S"),
                        "Pending",
                        "Break"
                    ])
                    self.scheduler.add_job(self.send_reminder, 'date', run_date=break_end, args=[reminder_id])
                    
                    return f"☕ {username}, enjoy your {duration}min break!"
            
            return f"⚠️ {username}, start a session first to take a break."
    
    def check_session_activity(self):
        now = datetime.now()
        if (now - self.last_activity_check).total_seconds() < 300:  # 5 min cooldown
            return
        self.last_activity_check = now
        
        with self.sheets_lock:
            sessions_sheet = sheets["Sessions"]
            sessions = sessions_sheet.get_all_records()
            
            for i, session in enumerate(sessions):
                if session['Status'] == 'Active':
                    last_activity = datetime.strptime(session['LastActivity'], "%Y-%m-%d %H:%M:%S")
                    if (now - last_activity).total_seconds() > 7200:  # 2 hours
                        sessions_sheet.update_cell(i + 2, sessions_sheet.find("Status").col, "Warning")
                        self.send_message(session['UserID'], 
                                         f"⚠️ {session['Username']}, you've been inactive for 2 hours! Type '!working' to continue")
                
                elif session['Status'] == 'Warning':
                    last_activity = datetime.strptime(session['LastActivity'], "%Y-%m-%d %H:%M:%S")
                    if (now - last_activity).total_seconds() > 1800:  # 30 minutes
                        # Apply penalty
                        self.update_user_xp(session['UserID'], -50)
                        sessions_sheet.delete_rows(i + 2)
                        self.send_message(session['UserID'], 
                                         f"⛔ {session['Username']}, session stopped due to inactivity. -50 XP penalty.")
    
    # ===== Reminder System =====
    def set_reminder(self, username, userid, args):
        if not args:
            return f"⚠️ {username}, specify reminder text and time"
        
        # Parse time (default 30 minutes)
        duration = 30
        time_match = re.search(r'(\d+)\s*(min|minutes|m|hour|hours|h)', args, re.IGNORECASE)
        if time_match:
            duration = int(time_match.group(1))
            if time_match.group(2).lower() in ['hour', 'hours', 'h']:
                duration *= 60
        
        # Clean reminder text
        reminder_text = re.sub(r'\d+\s*(min|minutes|m|hour|hours|h)', '', args, flags=re.IGNORECASE).strip()
        if not reminder_text:
            reminder_text = "Your reminder!"
        
        # Schedule reminder
        remind_time = datetime.now() + timedelta(minutes=duration)
        reminder_id = str(uuid.uuid4())
        
        with self.sheets_lock:
            reminders_sheet = sheets["Reminders"]
            reminders_sheet.append_row([
                reminder_id,
                userid,
                username,
                reminder_text,
                remind_time.strftime("%Y-%m-%d %H:%M:%S"),
                "Pending",
                "User"
            ])
            self.scheduler.add_job(self.send_reminder, 'date', run_date=remind_time, args=[reminder_id])
        
        return f"⏰ {username}, reminder set for in {duration} minutes!"
    
    def send_reminder(self, reminder_id):
        with self.sheets_lock:
            reminders_sheet = sheets["Reminders"]
            reminders = reminders_sheet.get_all_records()
            
            for i, reminder in enumerate(reminders):
                if reminder['ReminderID'] == reminder_id and reminder['Status'] == 'Pending':
                    self.send_message(reminder['UserID'], f"🔔 {reminder['Username']}, reminder: {reminder['Message']}")
                    reminders_sheet.update_cell(i + 2, reminders_sheet.find("Status").col, "Sent")
                    break

    def send_scheduled_reminders(self):
        print("Scheduled reminder check placeholder (to be implemented)")

    
    # ===== Task System =====
    def add_task(self, username, userid, args):
        if not args or len(args.strip()) < 3:
            return f"⚠️ {username}, specify your task"
        
        with self.sheets_lock:
            tasks_sheet = sheets["Tasks"]
            tasks = tasks_sheet.get_all_records()
            
            # Check for existing active task
            for task in tasks:
                if str(task['UserID']) == str(userid) and task['Status'] == 'Active':
                    return f"⚠️ {username}, complete your current task first!"
            
            # Add new task
            task_id = str(uuid.uuid4())
            tasks_sheet.append_row([
                task_id,
                userid,
                username,
                args.strip(),
                "Active",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "",
                0
            ])
            return f"📝 {username}, task added: '{args.strip()}'"
    
    def complete_task(self, username, userid, args):
        with self.sheets_lock:
            tasks_sheet = sheets["Tasks"]
            tasks = tasks_sheet.get_all_records()
            activities_sheet = sheets["Activities"]
            
            for i, task in enumerate(tasks):
                if str(task['UserID']) == str(userid) and task['Status'] == 'Active':
                    # Update task
                    tasks_sheet.update_cell(i + 2, tasks_sheet.find("Status").col, "Completed")
                    tasks_sheet.update_cell(i + 2, tasks_sheet.find("CompletedDate").col, 
                                          datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    tasks_sheet.update_cell(i + 2, tasks_sheet.find("XPEarned").col, 15)
                    
                    # Log activity
                    activity_id = str(uuid.uuid4())
                    activities_sheet.append_row([
                        activity_id,
                        userid,
                        username,
                        "TaskCompleted",
                        15,
                        "",
                        f"Completed: {task['TaskName']}",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        datetime.now().strftime("%Y-%m")
                    ])
                    
                    # Award XP
                    self.update_user_xp(userid, 15)
                    return f"✅ {username}, task completed! +15 XP"
            
            return f"⚠️ {username}, no active task found."
    
    def remove_task(self, username, userid, args):
        with self.sheets_lock:
            tasks_sheet = sheets["Tasks"]
            tasks = tasks_sheet.get_all_records()
            
            for i, task in enumerate(tasks):
                if str(task['UserID']) == str(userid) and task['Status'] == 'Active':
                    tasks_sheet.update_cell(i + 2, tasks_sheet.find("Status").col, "Removed")
                    return f"🗑️ {username}, task removed"
            
            return f"⚠️ {username}, no active task found."
    
    def completed_tasks(self, username, userid, args):
        with self.sheets_lock:
            tasks_sheet = sheets["Tasks"]
            tasks = tasks_sheet.get_all_records()
            
            user_tasks = [t for t in tasks if str(t['UserID']) == str(userid) and t['Status'] == 'Completed']
            user_tasks.sort(key=lambda x: x['CompletedDate'], reverse=True)
            recent_tasks = user_tasks[:3]
            
            if not recent_tasks:
                return f"📝 {username}, no completed tasks yet"
            
            response = f"✅ {username}'s recent completions:\n"
            for i, task in enumerate(recent_tasks, 1):
                date = task['CompletedDate'][:10] if task['CompletedDate'] else "Unknown"
                response += f"{i}. {task['TaskName']} ({date})\n"
            
            return response.strip()
    
    def pending_task(self, username, userid, args):
        with self.sheets_lock:
            tasks_sheet = sheets["Tasks"]
            tasks = tasks_sheet.get_all_records()
            
            for task in tasks:
                if str(task['UserID']) == str(userid) and task['Status'] == 'Active':
                    return f"📝 {username}, current task: '{task['TaskName']}'"
            
            return f"✅ {username}, no pending tasks"
    
    # ===== Goal System =====
    def set_goal(self, username, userid, args):
        if not args or len(args.strip()) < 3:
            # Show current goal if exists
            with self.sheets_lock:
                goals_sheet = sheets["Goals"]
                goals = goals_sheet.get_all_records()
                
                for goal in goals:
                    if str(goal['UserID']) == str(userid) and goal['Status'] == 'Active':
                        return f"🎯 {username}, current goal: '{goal['Goal']}'"
                
                return f"⚠️ {username}, specify your goal"
        
        # Add new goal
        with self.sheets_lock:
            goals_sheet = sheets["Goals"]
            goal_id = str(uuid.uuid4())
            goals_sheet.append_row([
                goal_id,
                userid,
                username,
                args.strip(),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Active"
            ])
            return f"🎯 {username}, goal set: '{args.strip()}'"
    
    def complete_goal(self, username, userid, args):
        with self.sheets_lock:
            goals_sheet = sheets["Goals"]
            goals = goals_sheet.get_all_records()
            activities_sheet = sheets["Activities"]
            
            for i, goal in enumerate(goals):
                if str(goal['UserID']) == str(userid) and goal['Status'] == 'Active':
                    # Update goal
                    goals_sheet.update_cell(i + 2, goals_sheet.find("Status").col, "Completed")
                    
                    # Log activity
                    activity_id = str(uuid.uuid4())
                    activities_sheet.append_row([
                        activity_id,
                        userid,
                        username,
                        "GoalCompleted",
                        25,
                        "",
                        f"Completed: {goal['Goal']}",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        datetime.now().strftime("%Y-%m")
                    ])
                    
                    # Award XP
                    self.update_user_xp(userid, 25)
                    return f"🎉 {username}, goal achieved! +25 XP"
            
            return f"⚠️ {username}, no active goal found"
    
    # ===== Attendance =====
    def mark_attendance(self, username, userid, args):
        today = datetime.now().date()
        
        with self.sheets_lock:
            activities_sheet = sheets["Activities"]
            activities = activities_sheet.get_all_records()
            
            # Check if already attended today
            for act in activities:
                if str(act['UserID']) == str(userid) and act['Action'] == 'Attendance':
                    try:
                        act_date = datetime.strptime(act['Timestamp'], "%Y-%m-%d %H:%M:%S").date()
                        if act_date == today:
                            return f"⚠️ {username}, attendance already marked"
                    except:
                        continue
            
            # Mark attendance
            activity_id = str(uuid.uuid4())
            activities_sheet.append_row([
                activity_id,
                userid,
                username,
                "Attendance",
                10,
                "",
                "Daily attendance",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                datetime.now().strftime("%Y-%m")
            ])
            
            # Award XP
            self.update_user_xp(userid, 10)
            
            # Update streak
            streak = self.calculate_streak(userid)
            row_idx, user = self.get_or_create_user(userid, username)
            if row_idx:
                with self.sheets_lock:
                    users_sheet = sheets["Users"]
                    users_sheet.update_cell(row_idx, users_sheet.find("CurrentStreak").col, streak)
            
            return f"✅ {username}, attendance marked! +10 XP 🔥 Streak: {streak} days"
    
    # ===== Statistics =====
    def user_summary(self, username, userid, args):
        row_idx, user = self.get_or_create_user(userid, username)
        if not user:
            return f"⚠️ Error loading data"
        
        with self.sheets_lock:
            tasks_sheet = sheets["Tasks"]
            tasks = tasks_sheet.get_all_records()
            
            completed_tasks = len([t for t in tasks if str(t['UserID']) == str(userid) and t['Status'] == 'Completed'])
            pending_tasks = len([t for t in tasks if str(t['UserID']) == str(userid) and t['Status'] == 'Active'])
            
            total_minutes = int(user.get('TotalStudyMinutes', 0))
            hours = total_minutes // 60
            minutes = total_minutes % 60
            
            return (f"📊 {username}'s Summary:\n"
                    f"• Total XP: {user['TotalXP']}\n"
                    f"• Rank: {user['Rank']}\n"
                    f"• Study Time: {hours}h {minutes}m\n"
                    f"• Streak: {user['CurrentStreak']} days\n"
                    f"• Completed Tasks: {completed_tasks}\n"
                    f"• Pending Tasks: {pending_tasks}\n"
                    f"• Badges: {user.get('Badges', 'None')}")
    
    def show_rank(self, username, userid, args):
        row_idx, user = self.get_or_create_user(userid, username)
        if not user:
            return f"⚠️ Error loading data"
        return f"🏅 {username}: {user['TotalXP']} XP | Rank: {user['Rank']}"
    
    def leaderboard(self, username, userid, args):
        with self.sheets_lock:
            users_sheet = sheets["Users"]
            users = users_sheet.get_all_records()
            
            sorted_users = sorted(users, key=lambda x: int(x.get('TotalXP', 0)), reverse=True)[:5]
            
            response = "🏆 Top 5:\n"
            for i, user in enumerate(sorted_users, 1):
                response += f"{i}. {user['Username']} - {user['TotalXP']} XP\n"
            
            return response.strip()
    
    def weekly_leaderboard(self, username, userid, args):
        one_week_ago = datetime.now() - timedelta(days=7)
        
        with self.sheets_lock:
            activities_sheet = sheets["Activities"]
            activities = activities_sheet.get_all_records()
            
            weekly_xp = {}
            for act in activities:
                try:
                    timestamp = datetime.strptime(act['Timestamp'], "%Y-%m-%d %H:%M:%S")
                    if timestamp >= one_week_ago:
                        user = act['Username']
                        xp = int(act.get('XPEarned', 0))
                        weekly_xp[user] = weekly_xp.get(user, 0) + xp
                except:
                    continue
            
            sorted_users = sorted(weekly_xp.items(), key=lambda x: x[1], reverse=True)[:5]
            
            response = "📆 Weekly Top 5:\n"
            for i, (user, xp) in enumerate(sorted_users, 1):
                response += f"{i}. {user} - {xp} XP\n"
            
            return response.strip()
    
    def monthly_leaderboard(self, username, userid, args):
        current_month = datetime.now().strftime("%Y-%m")
        
        with self.sheets_lock:
            activities_sheet = sheets["Activities"]
            activities = activities_sheet.get_all_records()
            
            monthly_xp = {}
            for act in activities:
                if act.get('Month') == current_month:
                    user = act['Username']
                    xp = int(act.get('XPEarned', 0))
                    monthly_xp[user] = monthly_xp.get(user, 0) + xp
            
            sorted_users = sorted(monthly_xp.items(), key=lambda x: x[1], reverse=True)[:5]
            
            response = "📅 Monthly Top 5:\n"
            for i, (user, xp) in enumerate(sorted_users, 1):
                response += f"{i}. {user} - {xp} XP\n"
            
            return response.strip()
    
    def show_badges(self, username, userid, args):
        row_idx, user = self.get_or_create_user(userid, username)
        if not user:
            return f"⚠️ Error loading data"
        
        badges = user.get('Badges', '')
        if not badges:
            return f"🎖️ {username}, no badges yet!"
        
        return f"🎖️ {username}'s badges: {badges}"
    
    def streak_info(self, username, userid, args):
        streak = self.calculate_streak(userid)
        if streak == 0:
            return f"🔥 {username}, start your streak with !attend"
        return f"🔥 {username}, current streak: {streak} days"
    
    # ===== AI Features =====
    def study_plan(self, username, userid, args):
        if not args:
            return f"⚠️ {username}, describe your study needs"
        
        plan = self.get_ai_response(f"Create a study plan for: {args}")
        
        with self.sheets_lock:
            plans_sheet = sheets["Plans"]
            plan_id = str(uuid.uuid4())
            plans_sheet.append_row([
                plan_id,
                userid,
                username,
                args,
                plan,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ])
        
        return f"📚 {username}, here's your study plan:\n{plan}"
    
    def my_plans(self, username, userid, args):
        with self.sheets_lock:
            plans_sheet = sheets["Plans"]
            plans = plans_sheet.get_all_records()
            
            user_plans = [p for p in plans if str(p['UserID']) == str(userid)]
            user_plans.sort(key=lambda x: x['CreatedDate'], reverse=True)
            recent_plans = user_plans[:3]
            
            if not recent_plans:
                return f"📚 {username}, no study plans yet"
            
            response = f"📋 {username}'s recent plans:\n"
            for i, plan in enumerate(recent_plans, 1):
                response += f"\n{i}. Request: {plan['PlanRequest'][:50]}...\n"
                response += f"   Plan: {plan['PlanResponse'][:50]}...\n"
            
            return response
    
    def ai_chat(self, username, userid, args):
        if not args:
            return f"🤖 {username}, ask me anything"
        
        return f"🤖 {self.get_ai_response(args)}"
    
    # ===== Other Features =====
    def report_user(self, username, userid, args):
        if not args or len(args.split()) < 2:
            return f"⚠️ {username}, use: '!report username reason'"
        
        parts = args.split(' ', 1)
        reported_user = parts[0]
        reason = parts[1] if len(parts) > 1 else "No reason"
        
        with self.sheets_lock:
            reports_sheet = sheets["Reports"]
            report_id = str(uuid.uuid4())
            reports_sheet.append_row([
                report_id,
                userid,
                username,
                reported_user,
                reason,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Pending"
            ])
        
        return f"📝 {username}, report submitted"
    
    def send_motivation(self, username, userid, args):
        quotes = [
            "The secret of getting ahead is getting started.",
            "Don't watch the clock; do what it does. Keep going.",
            "The future belongs to those who believe in their dreams.",
            "You are never too old to set a new goal.",
            "Believe you can and you're halfway there."
        ]
        quote = quotes[hash(username + str(datetime.now().date())) % len(quotes)]
        return f"💫 {username}: {quote}"
    
    def focus_mode(self, username, userid, args):
        duration = 25  # Default Pomodoro
        if args:
            try:
                duration = int(re.search(r'(\d+)', args).group(1))
                duration = min(duration, 180)  # Max 3 hours
            except:
                pass
        
        # Schedule reminder
        focus_end = datetime.now() + timedelta(minutes=duration)
        reminder_id = str(uuid.uuid4())
        
        with self.sheets_lock:
            reminders_sheet = sheets["Reminders"]
            reminders_sheet.append_row([
                reminder_id,
                userid,
                username,
                f"Focus session complete! Take a break.",
                focus_end.strftime("%Y-%m-%d %H:%M:%S"),
                "Pending",
                "Focus"
            ])
            self.scheduler.add_job(self.send_reminder, 'date', run_date=focus_end, args=[reminder_id])
        
        return f"🎯 {username}, focus mode for {duration} minutes!"
    
    def show_help(self, username, userid, args):
        return (
            "📚 StudyPlus Commands:\n\n"
            "!start - Begin study session\n"
            "!stop - End session & get XP\n"
            "!working - Confirm activity\n"
            "!break [min] - Take break\n"
            "!remind [msg] in [time] - Set reminder\n"
            "!task [desc] - Add task\n"
            "!done - Complete task\n"
            "!remove - Remove task\n"
            "!comtask - Completed tasks\n"
            "!goal [desc] - Set goal\n"
            "!complete - Complete goal\n"
            "!attend - Mark attendance\n"
            "!rank - Your XP & rank\n"
            "!summary - Progress summary\n"
            "!top - Leaderboard\n"
            "!weeklytop - Weekly leaders\n"
            "!monthtop - Monthly leaders\n"
            "!plan [desc] - AI study plan\n"
            "!myplans - Saved plans\n"
            "!ai [question] - Ask AI\n"
            "!focus [min] - Focus timer\n"
            "!motivation - Motivational quote\n"
            "!report [user] [reason] - Report user\n"
            "!help - Show this menu"
        )
    
    # ===== YouTube Integration =====
    def check_live_stream(self):
        if not youtube_service:
            return
        
        try:
            request = youtube_service.search().list(
                part="snippet",
                channelId=YT_CHANNEL_ID,
                eventType="live",
                type="video"
            )
            response = request.execute()
            
            if response.get('items'):
                live_video_id = response['items'][0]['id']['videoId']
                
                # Get live chat ID
                video_request = youtube_service.videos().list(
                    part="liveStreamingDetails",
                    id=live_video_id
                )
                video_response = video_request.execute()
                if video_response.get('items'):
                    self.live_chat_id = video_response['items'][0]['liveStreamingDetails']['activeLiveChatId']
                    self.last_chat_id = None
        except Exception as e:
            print(f"Error checking live stream: {str(e)}")
    
    def monitor_chat(self):
        while True:
            if not self.live_chat_id or not youtube_service:
                time.sleep(60)
                continue
            
            try:
                request = youtube_service.liveChatMessages().list(
                    liveChatId=self.live_chat_id,
                    part="snippet,authorDetails"
                )
                if self.last_chat_id:
                    request.pageToken = self.last_chat_id
                
                response = request.execute()
                self.last_chat_id = response.get('nextPageToken')
                
                for item in response['items']:
                    message = item['snippet']['displayMessage']
                    user = item['authorDetails']['displayName']
                    user_id = item['authorDetails']['channelId']
                    
                    if message.startswith('!'):
                        self.process_command(message[1:], user, user_id)
                
                # Wait for next poll
                sleep_time = response.get('pollingIntervalMillis', 5000) / 1000
                time.sleep(sleep_time)
            except Exception as e:
                print(f"Chat monitoring error: {str(e)}")
                time.sleep(10)
    
    def send_message(self, user_id, message):
        if not self.live_chat_id or not youtube_service:
            return
        
        try:
            youtube_service.liveChatMessages().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": self.live_chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {
                            "messageText": message
                        }
                    }
                }
            ).execute()
        except Exception as e:
            print(f"Error sending message: {str(e)}")
    
    def process_command(self, command, username, user_id):
        cmd_parts = command.split(maxsplit=1)
        cmd = cmd_parts[0].lower()
        args = cmd_parts[1] if len(cmd_parts) > 1 else ""
        
        commands = {
            'start': self.start_session,
            'stop': self.stop_session,
            'working': self.confirm_working,
            'break': self.start_break,
            'remind': self.set_reminder,
            'task': self.add_task,
            'done': self.complete_task,
            'remove': self.remove_task,
            'comtask': self.completed_tasks,
            'pending': self.pending_task,
            'rank': self.show_rank,
            'top': self.leaderboard,
            'weeklytop': self.weekly_leaderboard,
            'monthtop': self.monthly_leaderboard,
            'goal': self.set_goal,
            'complete': self.complete_goal,
            'summary': self.user_summary,
            'plan': self.study_plan,
            'myplans': self.my_plans,
            'report': self.report_user,
            'help': self.show_help,
            'ai': self.ai_chat,
            'streak': self.streak_info,
            'badges': self.show_badges,
            'motivation': self.send_motivation,
            'focus': self.focus_mode,
            'attend': self.mark_attendance
        }
        
        if cmd in commands:
            response = commands[cmd](username, user_id, args)
            self.send_message(user_id, response)
    
    # ===== Cleanup =====
    def cleanup_old_sessions(self):
        with self.sheets_lock:
            sessions_sheet = sheets["Sessions"]
            sessions = sessions_sheet.get_all_records()
            now = datetime.now()
            
            rows_to_delete = []
            for i, session in enumerate(sessions):
                try:
                    last_activity = datetime.strptime(session['LastActivity'], "%Y-%m-%d %H:%M:%S")
                    if (now - last_activity).total_seconds() > 86400:  # 24 hours
                        rows_to_delete.append(i + 2)
                except:
                    continue
            
            for row in reversed(rows_to_delete):
                sessions_sheet.delete_rows(row)
    
    def cleanup_old_reminders(self):
        with self.sheets_lock:
            reminders_sheet = sheets["Reminders"]
            reminders = reminders_sheet.get_all_records()
            one_week_ago = datetime.now() - timedelta(days=7)
            
            rows_to_delete = []
            for i, reminder in enumerate(reminders):
                try:
                    remind_time = datetime.strptime(reminder['ReminderTime'], "%Y-%m-%d %H:%M:%S")
                    if reminder['Status'] == 'Sent' and remind_time < one_week_ago:
                        rows_to_delete.append(i + 2)
                except:
                    continue
            
            for row in reversed(rows_to_delete):
                reminders_sheet.delete_rows(row)

# ========== Initialize Bot ==========
bot = StudyBot()

# ========== Flask Routes ==========
@app.route('/')
def home():
    return "StudyPlus Bot is running! 🚀"

# ========== Run Application ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    
    # Start YouTube monitoring in background thread
    if youtube_service:
        threading.Thread(target=bot.monitor_chat, daemon=True).start()
    
    app.run(host="0.0.0.0", port=port)
