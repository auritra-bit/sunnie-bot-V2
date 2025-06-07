from flask import Flask, request, jsonify
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import time
from threading import Thread, Lock
import os
import re
import requests
import json
from apscheduler.schedulers.background import BackgroundScheduler
import uuid

app = Flask(__name__)

# Track active sessions and background scheduler
sessions = {}
session_lock = Lock()
scheduler = BackgroundScheduler()
scheduler.start()

# === Google Sheet Setup ===
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
scope = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]
client = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
workbook = client.open("StudyData")

# Access different sheets
users_sheet = workbook.worksheet("Users")
sessions_sheet = workbook.worksheet("Sessions") 
activities_sheet = workbook.worksheet("Activities")
tasks_sheet = workbook.worksheet("Tasks")
goals_sheet = workbook.worksheet("Goals")
reminders_sheet = workbook.worksheet("Reminders")
reports_sheet = workbook.worksheet("Reports")
plans_sheet = workbook.worksheet("Plans")

# === Free AI API Setup (Using Hugging Face Inference API) ===
HF_API_URL = "https://api-inference.huggingface.co/models/microsoft/DialoGPT-medium"
HF_HEADERS = {"Authorization": "Bearer YOUR_HF_TOKEN"}  # Replace with your HF token

def get_sessions_with_headers():
    """Returns all rows from Sessions sheet with headers, padding missing cells."""
    rows = sessions_sheet.get_all_values()
    if len(rows) < 2:
        return []
    headers = rows[0]
    return [
        dict(zip(headers, row + [''] * (len(headers) - len(row))))
        for row in rows[1:]
    ]


def safe_get_all_records(sheet):
    try:
        values = sheet.get_all_values()
        if len(values) < 2:
            return []  # Only headers, no data
        return sheet.get_all_records()
    except Exception as e:
        print(f"Error reading sheet: {e}")
        return []


def get_ai_response(prompt, max_chars=180):
    """Get AI response using Hugging Face free API"""
    try:
        payload = {"inputs": prompt}
        response = requests.post(HF_API_URL, headers=HF_HEADERS, json=payload)
        if response.status_code == 200:
            result = response.json()
            if isinstance(result, list) and len(result) > 0:
                ai_text = result[0].get('generated_text', '')
                # Clean and truncate response
                ai_text = ai_text.replace(prompt, '').strip()
                return ai_text[:max_chars] + "..." if len(ai_text) > max_chars else ai_text
        return "Sorry, AI is busy right now. Try again later! 🤖"
    except:
        return "AI service unavailable. Please try again! 🤖"

# === Rank System ===
def get_rank(xp):
    xp = int(xp)
    if xp >= 1000: return "👑 Legend"
    elif xp >= 500: return "📘 Scholar"
    elif xp >= 300: return "📗 Master"
    elif xp >= 150: return "📙 Intermediate"
    elif xp >= 50: return "📕 Beginner"
    else: return "🍼 Newbie"

# === Badge System ===
def get_badges(total_minutes):
    badges = []
    if total_minutes >= 50: badges.append("🥉 Bronze Mind")
    if total_minutes >= 110: badges.append("🥈 Silver Brain")
    if total_minutes >= 150: badges.append("🥇 Golden Genius")
    if total_minutes >= 240: badges.append("🔷 Diamond Crown")
    if total_minutes >= 500: badges.append("💎 Master Scholar")
    return badges

# === User Management ===
def get_or_create_user(userid, username):
    """Get user data or create new user"""
    try:
        users = safe_get_all_records(users_sheet)
        for i, user in enumerate(users):
            if str(user['UserID']) == str(userid):
                return i + 2, user  # Return row index and user data
        
        # Create new user
        new_row = [username, userid, 0, 0, 0, "🍼 Newbie", 
                  datetime.now().strftime("%Y-%m-%d"), 
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                  "Inactive", 0]
        users_sheet.append_row(new_row)
        return len(users) + 2, {
            'Username': username, 'UserID': userid, 'TotalXP': 0,
            'CurrentStreak': 0, 'TotalStudyMinutes': 0, 'Rank': "🍼 Newbie"
        }
    except:
        return None, None

def update_user_xp(userid, xp_to_add):
    """Update user's total XP and rank"""
    row_idx, user = get_or_create_user(userid, "")
    if row_idx:
        new_xp = int(user.get('TotalXP', 0)) + xp_to_add
        new_rank = get_rank(new_xp)
        users_sheet.update_cell(row_idx, 3, new_xp)  # TotalXP
        users_sheet.update_cell(row_idx, 6, new_rank)  # Rank

def calculate_streak(userid):
    """Calculate daily attendance streak"""
    try:
        activities = safe_get_all_records(activities_sheet)
        dates = set()
        for activity in activities:
            if str(activity['UserID']) == str(userid) and activity['Action'] == 'Attendance':
                try:
                    date = datetime.strptime(activity['Timestamp'], "%Y-%m-%d %H:%M:%S").date()
                    dates.add(date)
                except ValueError:
                    pass
        
        if not dates:
            return 0
        
        streak = 0
        today = datetime.now().date()
        
        for i in range(0, 365):
            day = today - timedelta(days=i)
            if day in dates:
                streak += 1
            else:
                break
        return streak
    except:
        return 0

# === Session Management ===
def check_session_activity():
    """Background task to check for inactive sessions"""
    try:
        sessions = safe_get_all_records(sessions_sheet)
        now = datetime.now()
        
        for i, session in enumerate(sessions):
            if session['Status'] == 'Active':
                last_activity = datetime.strptime(session['LastActivity'], "%Y-%m-%d %H:%M:%S")
                time_diff = (now - last_activity).total_seconds() / 3600  # hours
                
                if time_diff >= 2:  # 2 hours inactive
                    # Send first warning
                    sessions_sheet.update_cell(i + 2, 5, 'Warning1')
                    send_warning_message(session['Username'], session['UserID'], 1)
                    
            elif session['Status'] == 'Warning1':
                last_activity = datetime.strptime(session['LastActivity'], "%Y-%m-%d %H:%M:%S")
                time_diff = (now - last_activity).total_seconds() / 60  # minutes
                
                if time_diff >= 30:  # 30 minutes after warning
                    # Apply penalty
                    apply_inactivity_penalty(session['UserID'], session['Username'])
                    sessions_sheet.delete_rows(i + 2)
    except Exception as e:
        print(f"Error checking sessions: {e}")

def send_warning_message(username, userid, warning_num):
    """Send warning message to user"""
    # This would integrate with your chat system
    print(f"⚠️ {username}, you've been inactive for 2 hours! Type !working to continue or your session will be penalized in 30 minutes.")

def apply_inactivity_penalty(userid, username):
    """Apply penalty for inactivity"""
    penalty_xp = -50  # Penalty amount
    update_user_xp(userid, penalty_xp)
    
    # Log penalty
    activities_sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        userid, username, "Inactivity Penalty", penalty_xp, 
        "", "Auto-penalty for inactivity", datetime.now().strftime("%Y-%m")
    ])

# Schedule session checking every 30 minutes
scheduler.add_job(check_session_activity, 'interval', minutes=30)

# === Reminder System ===
def parse_time_from_text(text):
    """Parse time from reminder text"""
    patterns = [
        (r'(\d+)\s*min', lambda m: datetime.now() + timedelta(minutes=int(m.group(1)))),
        (r'(\d+)\s*hour', lambda m: datetime.now() + timedelta(hours=int(m.group(1)))),
        (r'(\d+)\s*PM', lambda m: datetime.now().replace(hour=int(m.group(1)) + 12, minute=0, second=0)),
        (r'(\d+)\s*AM', lambda m: datetime.now().replace(hour=int(m.group(1)), minute=0, second=0)),
    ]
    
    for pattern, time_func in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return time_func(match)
    
    # Default: 50 minutes later
    return datetime.now() + timedelta(minutes=50)

def send_reminder(reminder_id):
    """Send scheduled reminder"""
    try:
        reminders = safe_get_all_records(reminders_sheet)
        for i, reminder in enumerate(reminders):
            if reminder['ReminderID'] == reminder_id and reminder['Status'] == 'Pending':
                # Mark as sent
                reminders_sheet.update_cell(i + 2, 6, 'Sent')
                # Send notification (integrate with your chat system)
                print(f"🔔 {reminder['Username']}, reminder: {reminder['ReminderText']}")
                break
    except Exception as e:
        print(f"Error sending reminder: {e}")

# === AI Plan Generator ===
def generate_study_plan(request_text):
    """Generate study plan using AI"""
    prompt = f"Create a short study plan for: {request_text}. Make it concise and actionable."
    return get_ai_response(prompt, 180)

# === ROUTES ===

@app.route("/attend")
def attend():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    now = datetime.now()
    today_date = now.date()

    # Check if already attended today
    activities = safe_get_all_records(activities_sheet)
    for activity in activities[::-1]:
        if (str(activity['UserID']) == str(userid) and 
            activity['Action'] == 'Attendance' and
            activity['Timestamp'].startswith(str(today_date))):
            return f"⚠️ {username}, attendance already recorded today! ✅"

    # Log attendance
    activities_sheet.append_row([
        now.strftime("%Y-%m-%d %H:%M:%S"), userid, username,
        "Attendance", 10, "", "Daily attendance", now.strftime("%Y-%m")
    ])
    
    # Update user XP
    update_user_xp(userid, 10)
    
    # Calculate streak
    streak = calculate_streak(userid)
    
    return f"✅ {username}, attendance logged! +10 XP 🔥 Streak: {streak} days"

@app.route("/start")
def start():
    try:
        username = request.args.get('user', '')
        userid = request.args.get('id', '')
        now = datetime.now()

        sessions = get_sessions_with_headers()
        for session in sessions:
            if str(session.get('UserID')) == str(userid):
                return f"⚠️ {username}, session already active! Use !stop first."

        new_row = [
            userid,
            username,
            now.strftime("%Y-%m-%d %H:%M:%S"),  # StartTime
            now.strftime("%Y-%m-%d %H:%M:%S"),  # LastActivity
            "Active",
            "",
            0
        ]

        sessions_sheet.append_row(new_row)
        print(f"[START] New session row added: {new_row}")

        return f"⏱️ {username}, study session started! Use !stop to end. 📚"

    except Exception as e:
        print(f"[ERROR in /start] {e}")
        return "❌ Failed to start session. Try again.", 500


@app.route("/stop")
def stop():
    try:
        username = request.args.get('user', '')
        userid = request.args.get('id', '')
        now = datetime.now()

        sessions = get_sessions_with_headers()

        for i, session in enumerate(sessions):
            if str(session.get('UserID')) != str(userid):
                continue

            start_str = session.get('StartTime', '').strip()
            if not start_str:
                print(f"[WARN] Skipping session with missing StartTime at row {i+2}")
                continue

            try:
                start_time = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
            except Exception as e:
                print(f"[WARN] Bad StartTime format at row {i+2}: {start_str} — {e}")
                continue

            duration_minutes = int((now - start_time).total_seconds() / 60)

            try:
                break_time = int(session.get('TotalBreakTime', 0)) or 0
            except:
                break_time = 0

            study_minutes = max(0, duration_minutes - break_time)
            xp_earned = study_minutes * 2

            activities_sheet.append_row([
                now.strftime("%Y-%m-%d %H:%M:%S"),
                userid,
                username,
                "StudySession",
                xp_earned,
                f"{study_minutes} min",
                f"Studied for {study_minutes} minutes",
                now.strftime("%Y-%m")
            ])

            update_user_xp(userid, xp_earned)
            sessions_sheet.delete_rows(i + 2)

            badges = get_badges(study_minutes)
            badge_msg = f" 🎖️ Badge unlocked: {badges[-1]}!" if badges else ""

            return f"🎓 {username}, studied {study_minutes}min, earned {xp_earned} XP!{badge_msg}"

        return f"⚠️ {username}, no active session found. Use !start first."

    except Exception as e:
        print(f"[ERROR in /stop] {e}")
        return "❌ Failed to stop session. Please try again.", 500




@app.route("/working")
def working():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    now = datetime.now()

    # Update session activity
    sessions = safe_get_all_records(sessions_sheet)
    for i, session in enumerate(sessions):
        if str(session['UserID']) == str(userid):
            sessions_sheet.update_cell(i + 2, 4, now.strftime("%Y-%m-%d %H:%M:%S"))  # LastActivity
            sessions_sheet.update_cell(i + 2, 5, "Active")  # Status
            return f"✅ {username}, session activity confirmed! Keep studying! 💪"
    
    return f"⚠️ {username}, no active session found."

@app.route("/break")
def take_break():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    msg = request.args.get('msg', '20')  # Default 20 minutes
    
    # Parse break duration
    duration_match = re.search(r'(\d+)', msg)
    duration = int(duration_match.group(1)) if duration_match else 20
    duration = min(duration, 120)  # Max 2 hours
    
    now = datetime.now()
    break_end = now + timedelta(minutes=duration)
    
    # Update session with break
    sessions = safe_get_all_records(sessions_sheet)
    for i, session in enumerate(sessions):
        if str(session['UserID']) == str(userid):
            current_break = int(session.get('TotalBreakTime', 0))
            sessions_sheet.update_cell(i + 2, 5, "Break")  # Status
            sessions_sheet.update_cell(i + 2, 6, break_end.strftime("%Y-%m-%d %H:%M:%S"))  # BreakEndTime
            sessions_sheet.update_cell(i + 2, 7, current_break + duration)  # TotalBreakTime
            
            # Schedule break end reminder
            reminder_id = str(uuid.uuid4())
            reminders_sheet.append_row([
                reminder_id, userid, username, f"Break time over! Back to studying 📚",
                break_end.strftime("%Y-%m-%d %H:%M:%S"), "Pending", "Break"
            ])
            scheduler.add_job(send_reminder, 'date', run_date=break_end, args=[reminder_id])
            
            return f"☕ {username}, enjoy your {duration}min break! I'll remind you when it's over."
    
    return f"⚠️ {username}, start a session first to take a break."

@app.route("/remind")
def set_reminder():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    msg = request.args.get('msg', '')
    
    if not msg:
        return f"⚠️ {username}, specify what to remind! E.g., !remind meeting in 30 min"
    
    # Parse reminder time
    reminder_time = parse_time_from_text(msg)
    reminder_text = re.sub(r'\d+\s*(min|hour|PM|AM)', '', msg, flags=re.IGNORECASE).strip()
    
    if not reminder_text:
        reminder_text = "Your reminder!"
    
    # Create reminder
    reminder_id = str(uuid.uuid4())
    reminders_sheet.append_row([
        reminder_id, userid, username, reminder_text,
        reminder_time.strftime("%Y-%m-%d %H:%M:%S"), "Pending", "Remind"
    ])
    
    # Schedule reminder
    scheduler.add_job(send_reminder, 'date', run_date=reminder_time, args=[reminder_id])
    
    time_diff = reminder_time - datetime.now()
    if time_diff.total_seconds() < 3600:  # Less than 1 hour
        time_str = f"{int(time_diff.total_seconds() / 60)} minutes"
    else:
        time_str = f"{int(time_diff.total_seconds() / 3600)} hours"
    
    return f"⏰ {username}, reminder set for '{reminder_text}' in {time_str}!"

@app.route("/task")
def add_task():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    msg = request.args.get('msg', '')

    if not msg or len(msg.strip()) < 3:
        return f"⚠️ {username}, specify your task! E.g., !task Physics Chapter 5"

    # Check for active tasks
    tasks = safe_get_all_records(tasks_sheet)
    active_tasks = [t for t in tasks if str(t['UserID']) == str(userid) and t['Status'] == 'Active']
    
    if active_tasks:
        return f"⚠️ {username}, complete your current task first! Use !done"

    # Create new task
    task_id = str(uuid.uuid4())[:8]
    tasks_sheet.append_row([
        task_id, userid, username, msg.strip(), "Active",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "", 0
    ])
    
    return f"✏️ {username}, task '{msg.strip()}' added! Use !done when complete."

@app.route("/done")
def mark_done():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')

    # Find active task
    tasks = safe_get_all_records(tasks_sheet)
    for i, task in enumerate(tasks):
        if str(task['UserID']) == str(userid) and task['Status'] == 'Active':
            # Mark as completed
            tasks_sheet.update_cell(i + 2, 5, 'Completed')  # Status
            tasks_sheet.update_cell(i + 2, 7, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))  # CompletedDate
            tasks_sheet.update_cell(i + 2, 8, 15)  # XPEarned
            
            # Log completion
            activities_sheet.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), userid, username,
                "TaskCompleted", 15, "", f"Completed: {task['TaskName']}", 
                datetime.now().strftime("%Y-%m")
            ])
            
            # Update user XP
            update_user_xp(userid, 15)
            
            return f"✅ {username}, task '{task['TaskName']}' completed! +15 XP 💪"
    
    return f"⚠️ {username}, no active task found. Use !task to add one."

@app.route("/remove")
def remove_task():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')

    # Find active task
    tasks = safe_get_all_records(tasks_sheet)
    for i, task in enumerate(tasks):
        if str(task['UserID']) == str(userid) and task['Status'] == 'Active':
            # Mark as removed
            tasks_sheet.update_cell(i + 2, 5, 'Removed')
            return f"🗑️ {username}, task '{task['TaskName']}' removed!"
    
    return f"⚠️ {username}, no active task found."

@app.route("/comtask")
def completed_tasks():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')

    # Get last 3 completed tasks
    tasks = safe_get_all_records(tasks_sheet)
    completed = [t for t in tasks if str(t['UserID']) == str(userid) and t['Status'] == 'Completed']
    recent_tasks = sorted(completed, key=lambda x: x['CompletedDate'], reverse=True)[:3]
    
    if not recent_tasks:
        return f"📝 {username}, no completed tasks yet. Keep going!"
    
    response = f"🏆 {username}'s recent completions:\n"
    for i, task in enumerate(recent_tasks, 1):
        date = task['CompletedDate'][:10]  # Just date part
        response += f"{i}. {task['TaskName']} ({date})\n"
    
    return response.strip()

@app.route("/pending")
def pending_task():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')

    # Find active task
    tasks = safe_get_all_records(tasks_sheet)
    for task in tasks:
        if str(task['UserID']) == str(userid) and task['Status'] == 'Active':
            return f"🕒 {username}, your current task: '{task['TaskName']}' - Use !done to complete"
    
    return f"✅ {username}, no pending tasks! Use !task to add one."

@app.route("/rank")
def rank():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')

    row_idx, user = get_or_create_user(userid, username)
    if user:
        return f"🏅 {username}: {user['TotalXP']} XP | Rank: {user['Rank']}"
    return f"⚠️ Error fetching rank data."

@app.route("/top")
def leaderboard():
    try:
        users = safe_get_all_records(users_sheet)
        sorted_users = sorted(users, key=lambda x: int(x.get('TotalXP', 0)), reverse=True)[:5]
        
        response = "🏆 Top 5 Learners:\n"
        for i, user in enumerate(sorted_users, 1):
            response += f"{i}. {user['Username']} - {user['TotalXP']} XP\n"
        
        return response.strip()
    except:
        return "Error loading leaderboard."

@app.route("/weeklytop")
def weekly_top():
    try:
        one_week_ago = datetime.now() - timedelta(days=7)
        activities = safe_get_all_records(activities_sheet)
        
        weekly_xp = {}
        for activity in activities:
            try:
                timestamp = datetime.strptime(activity['Timestamp'], "%Y-%m-%d %H:%M:%S")
                if timestamp >= one_week_ago:
                    user = activity['Username']
                    xp = int(activity.get('XPEarned', 0))
                    weekly_xp[user] = weekly_xp.get(user, 0) + xp
            except:
                continue
        
        sorted_users = sorted(weekly_xp.items(), key=lambda x: x[1], reverse=True)[:5]
        
        response = "📆 Weekly Top 5:\n"
        for i, (user, xp) in enumerate(sorted_users, 1):
            response += f"{i}. {user} - {xp} XP\n"
        
        return response.strip()
    except:
        return "Error loading weekly leaderboard."

@app.route("/monthtop")
def monthly_top():
    try:
        current_month = datetime.now().strftime("%Y-%m")
        activities = safe_get_all_records(activities_sheet)
        
        monthly_xp = {}
        for activity in activities:
            if activity.get('Month') == current_month:
                user = activity['Username']
                xp = int(activity.get('XPEarned', 0))
                monthly_xp[user] = monthly_xp.get(user, 0) + xp
        
        sorted_users = sorted(monthly_xp.items(), key=lambda x: x[1], reverse=True)[:5]
        
        response = "📅 Monthly Top 5:\n"
        for i, (user, xp) in enumerate(sorted_users, 1):
            response += f"{i}. {user} - {xp} XP\n"
        
        return response.strip()
    except:
        return "Error loading monthly leaderboard."

@app.route("/goal")
def goal():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    msg = request.args.get('msg', '')

    if msg.strip():
        # Set new goal
        goals_sheet.append_row([
            userid, username, msg.strip(), 
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "Active"
        ])
        return f"🎯 {username}, goal set: '{msg.strip()}' - Use !complete to mark achieved!"
    else:
        # Show current goal
        goals = safe_get_all_records(goals_sheet)
        for goal in goals[::-1]:
            if str(goal['UserID']) == str(userid) and goal['Status'] == 'Active':
                return f"🎯 {username}, current goal: '{goal['Goal']}' - Use !complete to mark achieved!"
        return f"⚠️ {username}, no active goal. Use !goal Your Goal to set one."

@app.route("/complete")
def complete_goal():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')

    # Find active goal
    goals = safe_get_all_records(goals_sheet)
    for i, goal in enumerate(goals):
        if str(goal['UserID']) == str(userid) and goal['Status'] == 'Active':
            # Mark as completed
            goals_sheet.update_cell(i + 2, 5, 'Completed')
            
            # Add XP reward
            activities_sheet.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), userid, username,
                "GoalCompleted", 25, "", f"Completed goal: {goal['Goal']}", 
                datetime.now().strftime("%Y-%m")
            ])
            
            update_user_xp(userid, 25)
            
            return f"🎉 {username}, goal achieved! +25 XP! 🎊"
    
    return f"⚠️ {username}, no active goal found."

@app.route("/summary")
def summary():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')

    try:
        # Get user data
        row_idx, user = get_or_create_user(userid, username)
        if not user:
            return f"⚠️ Error loading summary"
        
        # Get tasks data
        tasks = safe_get_all_records(tasks_sheet)
        completed_tasks = len([t for t in tasks if str(t['UserID']) == str(userid) and t['Status'] == 'Completed'])
        pending_tasks = len([t for t in tasks if str(t['UserID']) == str(userid) and t['Status'] == 'Active'])
        
        # Calculate time
        total_minutes = int(user.get('TotalStudyMinutes', 0))
        hours = total_minutes // 60
        minutes = total_minutes % 60
        
        return (f"📊 {username}'s Summary:\n"
                f"⏱️ Total Study Time: {hours}h {minutes}m\n"
                f"⚜️ Total XP: {user['TotalXP']}\n"
                f"🔥 Streak: {user['CurrentStreak']} days\n"
                f"✅ Completed Tasks: {completed_tasks}\n"
                f"🕒 Pending Tasks: {pending_tasks}\n"
                f"🏅 Rank: {user['Rank']}")
    except:
        return f"⚠️ Error loading summary"

@app.route("/plan")
def study_plan():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    msg = request.args.get('msg', '')
    
    if not msg:
        return f"⚠️ {username}, describe your study needs! E.g., !plan exam in 3 days, math physics"
    
    # Generate AI plan
    ai_plan = generate_study_plan(msg)
    
    # Save plan
    plan_id = str(uuid.uuid4())[:8]
    plans_sheet.append_row([
        plan_id, userid, username, msg.strip(), ai_plan,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ])
    
    return f"📚 {username}, here's your study plan:\n{ai_plan}\n\n💡 Plan saved! Use !myplans to view saved plans."

@app.route("/myplans")
def my_plans():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    
    try:
        plans = safe_get_all_records(plans_sheet)
        user_plans = [p for p in plans if str(p['UserID']) == str(userid)]
        recent_plans = sorted(user_plans, key=lambda x: x['CreatedDate'], reverse=True)[:3]
        
        if not recent_plans:
            return f"📚 {username}, no study plans yet. Use !plan to create one!"
        
        response = f"📋 {username}'s Recent Plans:\n"
        for i, plan in enumerate(recent_plans, 1):
            date = plan['CreatedDate'][:10]  # Just date part
            response += f"\n{i}. Request: {plan['PlanRequest']}\n"
            response += f"   Date: {date}\n"
            response += f"   Plan: {plan['PlanResponse'][:100]}...\n"
        
        return response.strip()
    except:
        return f"⚠️ {username}, error loading plans."

@app.route("/report")
def report_user():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    msg = request.args.get('msg', '')
    
    if not msg or len(msg.split()) < 2:
        return f"⚠️ {username}, use: !report username reason"
    
    parts = msg.split(' ', 1)
    reported_user = parts[0]
    reason = parts[1] if len(parts) > 1 else "No reason specified"
    
    # Create report
    report_id = str(uuid.uuid4())[:8]
    reports_sheet.append_row([
        report_id, userid, username, reported_user, reason,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "Pending"
    ])
    
    return f"📝 {username}, report submitted against {reported_user}. Moderators will review."

@app.route("/help")
def help_command():
    username = request.args.get('user', '')
    
    help_text = f"""🤖 StudyPlus Commands for {username}:

📚 **Study Sessions:**
!start - Begin study session
!stop - End session & get XP
!working - Confirm you're studying
!break 20 - Take 20min break

📋 **Tasks:**
!task Math Chapter 5 - Add task
!done - Complete current task
!remove - Remove current task
!pending - Show current task
!comtask - Show completed tasks

🎯 **Goals & Progress:**
!goal Study 5hrs daily - Set goal
!complete - Mark goal achieved
!rank - Your XP & rank
!summary - Full progress summary

🏆 **Leaderboards:**
!top - Top 5 all-time
!weeklytop - Weekly leaders
!monthtop - Monthly leaders

⏰ **Reminders:**
!remind meeting in 30 min
!remind study break in 1 hour

📚 **AI Study Plans:**
!plan exam in 3 days math physics
!myplans - View saved plans

📅 **Daily:**
!attend - Mark daily attendance

📝 **Other:**
!report username reason - Report user
!help - Show this menu

💡 **XP System:**
• Daily attendance: +10 XP
• Study sessions: +2 XP/min
• Task completion: +15 XP
• Goal achievement: +25 XP

🏅 **Ranks:**
🍼 Newbie (0+) → 📕 Beginner (50+) → 📙 Intermediate (150+) → 📗 Master (300+) → 📘 Scholar (500+) → 👑 Legend (1000+)

Keep studying! 💪"""
    
    return help_text

@app.route("/ai")
def ai_chat():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    msg = request.args.get('msg', '')
    
    if not msg:
        return f"🤖 {username}, ask me anything! E.g., !ai How to study effectively?"
    
    # Get AI response
    ai_response = get_ai_response(f"Study helper question: {msg}", 200)
    
    return f"🤖 StudyBot: {ai_response}"

@app.route("/streak")
def streak_info():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    
    streak = calculate_streak(userid)
    
    if streak == 0:
        return f"🔥 {username}, start your streak! Use !attend daily to build it up."
    elif streak == 1:
        return f"🔥 {username}, 1 day streak! Keep going to build momentum! 💪"
    elif streak < 7:
        return f"🔥 {username}, {streak} days streak! You're building a habit! 🚀"
    elif streak < 30:
        return f"🔥 {username}, {streak} days streak! Amazing consistency! 🌟"
    else:
        return f"🔥 {username}, {streak} days streak! You're a legend! 👑"

@app.route("/badges")
def show_badges():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    
    # Get user's total study minutes
    row_idx, user = get_or_create_user(userid, username)
    if not user:
        return f"⚠️ Error loading badge data"
    
    total_minutes = int(user.get('TotalStudyMinutes', 0))
    badges = get_badges(total_minutes)
    
    if not badges:
        return f"🎖️ {username}, no badges yet! Study 50+ minutes to earn your first badge!"
    
    response = f"🎖️ {username}'s Badges:\n"
    for badge in badges:
        response += f"• {badge}\n"
    
    # Show next badge target
    if total_minutes < 50:
        response += f"\n🎯 Next: Study {50 - total_minutes} more minutes for 🥉 Bronze Mind!"
    elif total_minutes < 110:
        response += f"\n🎯 Next: Study {110 - total_minutes} more minutes for 🥈 Silver Brain!"
    elif total_minutes < 150:
        response += f"\n🎯 Next: Study {150 - total_minutes} more minutes for 🥇 Golden Genius!"
    elif total_minutes < 240:
        response += f"\n🎯 Next: Study {240 - total_minutes} more minutes for 🔷 Diamond Crown!"
    elif total_minutes < 500:
        response += f"\n🎯 Next: Study {500 - total_minutes} more minutes for 💎 Master Scholar!"
    else:
        response += "\n👑 You've earned all badges! Keep studying!"
    
    return response.strip()

@app.route("/stats")
def detailed_stats():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    
    try:
        # Get user data
        row_idx, user = get_or_create_user(userid, username)
        if not user:
            return f"⚠️ Error loading stats"
        
        # Get this week's activity
        one_week_ago = datetime.now() - timedelta(days=7)
        activities = safe_get_all_records(activities_sheet)
        
        weekly_xp = 0
        weekly_minutes = 0
        weekly_tasks = 0
        
        for activity in activities:
            try:
                if (str(activity['UserID']) == str(userid) and 
                    datetime.strptime(activity['Timestamp'], "%Y-%m-%d %H:%M:%S") >= one_week_ago):
                    
                    weekly_xp += int(activity.get('XPEarned', 0))
                    
                    if activity['Action'] == 'StudySession':
                        duration_str = activity.get('Duration', '0 min')
                        minutes = int(re.search(r'(\d+)', duration_str).group(1)) if re.search(r'(\d+)', duration_str) else 0
                        weekly_minutes += minutes
                    elif activity['Action'] == 'TaskCompleted':
                        weekly_tasks += 1
            except:
                continue
        
        # Calculate averages
        weekly_hours = weekly_minutes // 60
        weekly_min_remainder = weekly_minutes % 60
        daily_avg_minutes = weekly_minutes // 7
        daily_avg_hours = daily_avg_minutes // 60
        daily_avg_min_remainder = daily_avg_minutes % 60
        
        total_minutes = int(user.get('TotalStudyMinutes', 0))
        total_hours = total_minutes // 60
        total_min_remainder = total_minutes % 60
        
        return (f"📊 {username}'s Detailed Stats:\n\n"
                f"🏆 **Overall:**\n"
                f"• Total XP: {user['TotalXP']}\n"
                f"• Rank: {user['Rank']}\n"
                f"• Total Study Time: {total_hours}h {total_min_remainder}m\n"
                f"• Current Streak: {user['CurrentStreak']} days\n\n"
                f"📅 **This Week:**\n"
                f"• XP Earned: {weekly_xp}\n"
                f"• Study Time: {weekly_hours}h {weekly_min_remainder}m\n"
                f"• Tasks Completed: {weekly_tasks}\n"
                f"• Daily Average: {daily_avg_hours}h {daily_avg_min_remainder}m\n\n"
                f"🎯 Keep up the great work! 💪")
    except:
        return f"⚠️ Error loading detailed stats"

@app.route("/leaderboard")
def full_leaderboard():
    username = request.args.get('user', '')
    type_param = request.args.get('type', 'all')  # all, weekly, monthly
    
    try:
        if type_param == 'weekly':
            one_week_ago = datetime.now() - timedelta(days=7)
            activities = safe_get_all_records(activities_sheet)
            
            weekly_xp = {}
            for activity in activities:
                try:
                    timestamp = datetime.strptime(activity['Timestamp'], "%Y-%m-%d %H:%M:%S")
                    if timestamp >= one_week_ago:
                        user = activity['Username']
                        xp = int(activity.get('XPEarned', 0))
                        weekly_xp[user] = weekly_xp.get(user, 0) + xp
                except:
                    continue
            
            sorted_users = sorted(weekly_xp.items(), key=lambda x: x[1], reverse=True)[:10]
            title = "📆 Weekly Leaderboard (Top 10):"
            
        elif type_param == 'monthly':
            current_month = datetime.now().strftime("%Y-%m")
            activities = safe_get_all_records(activities_sheet)
            
            monthly_xp = {}
            for activity in activities:
                if activity.get('Month') == current_month:
                    user = activity['Username']
                    xp = int(activity.get('XPEarned', 0))
                    monthly_xp[user] = monthly_xp.get(user, 0) + xp
            
            sorted_users = sorted(monthly_xp.items(), key=lambda x: x[1], reverse=True)[:10]
            title = "📅 Monthly Leaderboard (Top 10):"
            
        else:  # all-time
            users = safe_get_all_records(users_sheet)
            sorted_users = [(u['Username'], int(u.get('TotalXP', 0))) for u in users]
            sorted_users = sorted(sorted_users, key=lambda x: x[1], reverse=True)[:10]
            title = "🏆 All-Time Leaderboard (Top 10):"
        
        if not sorted_users:
            return f"📊 No data available for {type_param} leaderboard."
        
        response = f"{title}\n"
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        
        for i, (user, xp) in enumerate(sorted_users):
            medal = medals[i] if i < len(medals) else f"{i+1}."
            response += f"{medal} {user} - {xp} XP\n"
        
        # Show user's position if not in top 10
        user_found = False
        for i, (user, xp) in enumerate(sorted_users):
            if user == username:
                user_found = True
                break
        
        if not user_found:
            # Find user's actual position
            all_users = sorted(weekly_xp.items() if type_param == 'weekly' 
                             else monthly_xp.items() if type_param == 'monthly' 
                             else [(u['Username'], int(u.get('TotalXP', 0))) for u in users_sheet.get_all_records()], 
                             key=lambda x: x[1], reverse=True)
            
            for i, (user, xp) in enumerate(all_users):
                if user == username:
                    response += f"\n📍 Your position: #{i+1} with {xp} XP"
                    break
        
        return response.strip()
    except:
        return "Error loading leaderboard."

@app.route("/motivation")
def motivation():
    username = request.args.get('user', '')
    
    motivational_quotes = [
        "🌟 Success is the sum of small efforts repeated day in and day out!",
        "💪 The expert in anything was once a beginner who refused to give up!",
        "🚀 Don't watch the clock; do what it does. Keep going!",
        "⭐ Your limitation—it's only your imagination!",
        "🔥 Push yourself, because no one else is going to do it for you!",
        "🏆 Great things never come from comfort zones!",
        "💎 Dream it. Wish it. Do it!",
        "🌈 Success doesn't just find you. You have to go out and get it!",
        "⚡ The harder you work for something, the greater you'll feel when you achieve it!",
        "🎯 Don't stop when you're tired. Stop when you're done!"
    ]
    
    quote = motivational_quotes[hash(username + str(datetime.now().date())) % len(motivational_quotes)]
    
    return f"{quote}\n\nKeep studying, {username}! You've got this! 📚✨"

@app.route("/focus")
def focus_mode():
    username = request.args.get('user', '')
    userid = request.args.get('id', '')
    msg = request.args.get('msg', '25')  # Default 25 minutes (Pomodoro)
    
    # Parse duration
    duration_match = re.search(r'(\d+)', msg)
    duration = int(duration_match.group(1)) if duration_match else 25
    duration = min(duration, 180)  # Max 3 hours
    
    # Set focus session reminder
    focus_end = datetime.now() + timedelta(minutes=duration)
    reminder_id = str(uuid.uuid4())
    
    reminders_sheet.append_row([
        reminder_id, userid, username, 
        f"🎯 Focus session complete! Time for a break! You studied for {duration} minutes.",
        focus_end.strftime("%Y-%m-%d %H:%M:%S"), "Pending", "Focus"
    ])
    
    # Schedule reminder
    scheduler.add_job(send_reminder, 'date', run_date=focus_end, args=[reminder_id])
    
    return f"🎯 {username}, focus mode activated for {duration} minutes! I'll remind you when it's time for a break. Stay focused! 📚🔥"

# === Error Handlers ===
@app.errorhandler(404)
def not_found(error):
    return "Command not found. Use !help for available commands.", 404

@app.errorhandler(500)
def internal_error(error):
    return "Internal server error. Please try again later.", 500

# === Cleanup Functions ===
def cleanup_old_sessions():
    """Clean up sessions older than 24 hours"""
    try:
        sessions = safe_get_all_records(sessions_sheet)
        now = datetime.now()
        
        rows_to_delete = []
        for i, session in enumerate(sessions):
            try:
                last_activity = datetime.strptime(session['LastActivity'], "%Y-%m-%d %H:%M:%S")
                time_diff = (now - last_activity).total_seconds() / 3600  # hours
                
                if time_diff >= 24:  # 24 hours old
                    rows_to_delete.append(i + 2)  # +2 for header row
            except:
                continue
        
        # Delete old sessions (in reverse order to maintain indices)
        for row_idx in reversed(rows_to_delete):
            sessions_sheet.delete_rows(row_idx)
            
    except Exception as e:
        print(f"Error cleaning up sessions: {e}")

def cleanup_old_reminders():
    """Clean up sent/expired reminders older than 7 days"""
    try:
        reminders = safe_get_all_records(reminders_sheet)
        one_week_ago = datetime.now() - timedelta(days=7)
        
        rows_to_delete = []
        for i, reminder in enumerate(reminders):
            try:
                reminder_time = datetime.strptime(reminder['ReminderTime'], "%Y-%m-%d %H:%M:%S")
                if (reminder['Status'] == 'Sent' and reminder_time < one_week_ago):
                    rows_to_delete.append(i + 2)
            except:
                continue
        
        # Delete old reminders
        for row_idx in reversed(rows_to_delete):
            reminders_sheet.delete_rows(row_idx)
            
    except Exception as e:
        print(f"Error cleaning up reminders: {e}")

# Schedule cleanup tasks
scheduler.add_job(cleanup_old_sessions, 'interval', hours=6)  # Every 6 hours
scheduler.add_job(cleanup_old_reminders, 'interval', hours=24)  # Daily

# === Main Application ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
