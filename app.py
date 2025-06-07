from flask import Flask, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import time
from threading import Thread
import os
import requests
import re
import pytz
import random

app = Flask(__name__)

# === Google Sheet Setup ===
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
scope = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]
creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
client = gspread.authorize(creds)

# Open spreadsheet and worksheets
spreadsheet = client.open("StudyPlusData")
main_sheet = spreadsheet.sheet1

# Get or create additional sheets
try:
    reports_sheet = spreadsheet.worksheet("Reports")
except gspread.WorksheetNotFound:
    reports_sheet = spreadsheet.add_worksheet("Reports", 100, 5)
    reports_sheet.append_row(["Timestamp", "Reporter", "Reported User", "Reason", "Resolved"])

try:
    sessions_sheet = spreadsheet.worksheet("ActiveSessions")
except gspread.WorksheetNotFound:
    sessions_sheet = spreadsheet.add_worksheet("ActiveSessions", 100, 6)
    sessions_sheet.append_row(["UserID", "Username", "StartTime", "LastActive", "Status", "Warnings"])

try:
    reminders_sheet = spreadsheet.worksheet("Reminders")
except gspread.WorksheetNotFound:
    reminders_sheet = spreadsheet.add_worksheet("Reminders", 100, 6)
    reminders_sheet.append_row(["UserID", "Username", "ReminderTime", "Message", "Triggered", "OriginalTime"])

try:
    penalties_sheet = spreadsheet.worksheet("Penalties")
except gspread.WorksheetNotFound:
    penalties_sheet = spreadsheet.add_worksheet("Penalties", 100, 5)
    penalties_sheet.append_row(["Timestamp", "UserID", "Username", "PenaltyXP", "Reason"])

# AI Configuration (Using free Hugging Face API)
HF_API_URL = "https://api-inference.huggingface.co/models/EleutherAI/gpt-neo-125M"
HF_API_TOKEN = os.environ.get("HF_API_TOKEN")

# === Helper Functions ===
def has_attended_today(userid):
    today = datetime.now().date()
    records = main_sheet.get_all_records()
    for row in records:
        if str(row['UserID']) == str(userid) and row['Action'] == 'Attendance':
            try:
                row_date = datetime.strptime(str(row['Timestamp']), "%Y-%m-%d %H:%M:%S").date()
                if row_date == today:
                    return True
            except ValueError:
                continue
    return False

def get_user_active_session(userid):
    sessions = sessions_sheet.get_all_records()
    for session in sessions:
        if str(session['UserID']) == str(userid) and session['Status'] == "active":
            return session
    return None

def parse_time_input(time_str):
    """Parse time input like '5 min', '2 hours', '3 PM'"""
    if not time_str:
        return datetime.now() + timedelta(minutes=50)
    
    time_str = time_str.lower()
    now = datetime.now()
    
    # Match numbers and units
    match = re.search(r'(\d+)\s*(min|minutes?|hrs?|hours?|days?|d)', time_str)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        
        if unit.startswith('min'):
            return now + timedelta(minutes=num)
        elif unit.startswith('hr') or unit.startswith('hour'):
            return now + timedelta(hours=num)
        elif unit.startswith('day') or unit == 'd':
            return now + timedelta(days=num)
    
    # Try to parse absolute time (e.g., "2 PM")
    try:
        time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', time_str)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            period = time_match.group(3)
            
            if period == 'pm' and hour < 12:
                hour += 12
            elif period == 'am' and hour == 12:
                hour = 0
                
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except:
        pass
    
    # Default to 50 minutes
    return now + timedelta(minutes=50)

# Replace your query_ai function with this improved version
def query_ai(prompt, max_length=200):
    if not HF_API_TOKEN:
        return "⚠️ AI service is currently offline. Please try later."
    
    headers = {"Authorization": f"Bearer {HF_API_TOKEN}"}
    payload = {
        "inputs": prompt,
        "parameters": {
            "max_length": max_length,
            "temperature": 0.7,
            "do_sample": True,
            "max_time": 10  # Timeout after 10 seconds
        }
    }
    
    try:
        # Try twice with delay
        for attempt in range(2):
            response = requests.post(HF_API_URL, headers=headers, json=payload, timeout=15)
            
            if response.status_code == 200:
                return response.json()[0]['generated_text'].strip()
            
            # Handle model loading
            if response.status_code == 503:
                wait_time = response.json().get('estimated_time', 10)
                time.sleep(wait_time + 2)  # Wait a bit longer than estimated
                continue
                
        return "⏳ AI is overloaded! Please try again in 30 seconds."
    
    except requests.exceptions.Timeout:
        return "⌛ AI response timed out. Try a simpler question!"
    except Exception as e:
        print(f"AI Error: {str(e)}")
        return "❌ AI glitch! Please try again later."

def apply_penalty(userid, username, xp_deduction, reason):
    """Apply XP penalty to a user"""
    # Add to penalties sheet
    penalties_sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        userid,
        username,
        xp_deduction,
        reason
    ])
    
    # Find user's XP rows and deduct
    records = main_sheet.get_all_records()
    for i, row in enumerate(records, start=2):  # start=2 because sheet rows are 1-indexed
        if str(row['UserID']) == str(userid) and row['XP'].isdigit():
            current_xp = int(row['XP'])
            new_xp = max(0, current_xp - xp_deduction)
            main_sheet.update_cell(i, 5, str(new_xp))  # Column 5 is XP
            break

# === Attendance Enforcement Decorator ===
def attendance_required(func):
    def wrapper(*args, **kwargs):
        userid = request.args.get('id') or ""
        if not userid or not has_attended_today(userid):
            username = request.args.get('user') or ""
            return f"⚠️ {username}, you must give attendance with !attend first."
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

# === Background Monitoring Thread ===
def monitor_sessions():
    while True:
        try:
            now = datetime.now()
            
            # Check inactive sessions
            sessions = sessions_sheet.get_all_records()
            for session in sessions:
                if session['Status'] == 'active':
                    last_active = datetime.strptime(session['LastActive'], "%Y-%m-%d %H:%M:%S")
                    time_diff = now - last_active
                    
                    if time_diff > timedelta(minutes=150):  # 2.5 hours
                        # Apply penalty
                        username = session['Username']
                        userid = session['UserID']
                        penalty = random.randint(30, 50)  # Random penalty between 30-50 XP
                        apply_penalty(userid, username, penalty, "Inactivity penalty")
                        
                        # Remove session
                        row_idx = sessions_sheet.find(session['UserID']).row
                        sessions_sheet.delete_rows(row_idx)
                        
                        # In real implementation, send penalty message to chat
                        print(f"⛔ {username} penalized {penalty} XP for inactivity!")
                    
                    elif time_diff > timedelta(minutes=120):  # 2 hours
                        # Update warning count
                        warnings = int(session.get('Warnings', 0)) + 1
                        row_idx = sessions_sheet.find(session['UserID']).row
                        sessions_sheet.update_cell(row_idx, 6, str(warnings))
                        
                        # Send warning message
                        print(f"⚠️ Inactivity warning #{warnings} for {session['Username']}")
            
            # Check reminders
            reminders = reminders_sheet.get_all_records()
            for reminder in reminders:
                if reminder['Triggered'] == "FALSE":
                    reminder_time = datetime.strptime(reminder['ReminderTime'], "%Y-%m-%d %H:%M:%S")
                    if now >= reminder_time:
                        # Mark as triggered
                        row_idx = reminders_sheet.find(reminder['UserID']).row
                        reminders_sheet.update_cell(row_idx, 5, "TRUE")
                        
                        # Send reminder to chat
                        print(f"🔔 Reminder for {reminder['Username']}: {reminder['Message']}")
            
            time.sleep(60)  # Check every minute
        except Exception as e:
            print(f"Monitoring error: {str(e)}")
            time.sleep(60)

# Start monitoring thread
Thread(target=monitor_sessions, daemon=True).start()

# === Rank System ===
def get_rank(xp):
    xp = int(xp)
    if xp >= 500:
        return "📘 Scholar"
    elif xp >= 300:
        return "📗 Master"
    elif xp >= 150:
        return "📙 Intermediate"
    elif xp >= 50:
        return "📕 Beginner"
    else:
        return "🍼 Newbie"

# === Badge System ===
def get_badges(total_minutes):
    badges = []
    if total_minutes >= 50:
        badges.append("🥉 Bronze Mind")
    if total_minutes >= 110:
        badges.append("🥈 Silver Brain")
    if total_minutes >= 150:
        badges.append("🥇 Golden Genius")
    if total_minutes >= 240:
        badges.append("🔷 Diamond Crown")
    return badges

# === Daily Streak ===
def calculate_streak(userid):
    records = main_sheet.get_all_records()
    dates = set()
    for row in records:
        if str(row['UserID']) == str(userid) and row['Action'] == 'Attendance':
            try:
                date = datetime.strptime(str(row['Timestamp']),
                                         "%Y-%m-%d %H:%M:%S").date()
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

# === ROUTES ===

# ✅ !attend
@app.route("/attend")
def attend():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""
    now = datetime.now()
    today_date = now.date()

    # Check if this user already gave attendance today
    records = main_sheet.get_all_records()
    for row in records[::-1]:
        if str(row['UserID']) == str(userid) and row['Action'] == 'Attendance':
            try:
                row_date = datetime.strptime(str(row['Timestamp']),
                                             "%Y-%m-%d %H:%M:%S").date()
                if row_date == today_date:
                    return f"⚠️ {username}, your attendance for today is already recorded! ✅"
            except ValueError:
                continue

    # Log new attendance
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    main_sheet.append_row(
        [username, userid, timestamp, "Attendance", "10", "", "", ""])
    streak = calculate_streak(userid)

    return f"✅ {username}, your attendance is logged and you earned 10 XP! 🔥 Daily Streak: {streak} days."

# ✅ !start
@app.route("/start")
@attendance_required
def start():
    username = request.args.get('user')
    userid = request.args.get('id')
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Check if a session is already running
    if get_user_active_session(userid):
        return f"⚠️ {username}, you already started a session. Use `!stop` before starting a new one."

    # Log a new session start
    sessions_sheet.append_row([userid, username, now, now, "active", "0"])
    return f"⏱️ {username}, your study session has started! Use `!stop` to end it. Happy studying 📚"

# ✅ !stop
@app.route("/stop")
@attendance_required
def stop():
    username = request.args.get('user')
    userid = request.args.get('id')
    now = datetime.now()

    # Get active session
    session = get_user_active_session(userid)
    if not session:
        return f"⚠️ {username}, you didn't start any session. Use `!start` to begin."

    # Calculate duration
    session_start = datetime.strptime(session['StartTime'], "%Y-%m-%d %H:%M:%S")
    duration_minutes = int((now - session_start).total_seconds() / 60)
    xp_earned = duration_minutes * 2

    # Add final study session row
    main_sheet.append_row([
        username, userid,
        now.strftime("%Y-%m-%d %H:%M:%S"),
        "Study Session", str(xp_earned),
        session_start.strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%Y-%m-%d %H:%M:%S"),
        f"{duration_minutes} min"
    ])

    # Remove from active sessions
    row_idx = sessions_sheet.find(userid).row
    sessions_sheet.delete_rows(row_idx)

    # Check badge
    badges = get_badges(duration_minutes)
    badge_message = f" 🎖 {username}, you unlocked a badge: {badges[-1]}! Keep it up" if badges else ""

    return f"👩🏻‍💻📓✍🏻 {username}, you studied for {duration_minutes} minutes and earned {xp_earned} XP.{badge_message}"

# ✅ !rank
@app.route("/rank")
@attendance_required
def rank():
    username = request.args.get('user')
    userid = request.args.get('id')

    records = main_sheet.get_all_records()
    total_xp = 0

    for row in records:
        if str(row['UserID']) == str(userid):
            try:
                total_xp += int(row['XP'])
            except ValueError:
                pass

    user_rank = get_rank(total_xp)
    return f"🏅 {username}, you have {total_xp} XP. Your rank is: {user_rank}"

# ✅ !top
@app.route("/top")
def leaderboard():
    records = main_sheet.get_all_records()
    xp_map = {}

    for row in records:
        name = row['Username']
        try:
            xp = int(row['XP'])
        except ValueError:
            continue

        if name in xp_map:
            xp_map[name] += xp
        else:
            xp_map[name] = xp

    sorted_users = sorted(xp_map.items(), key=lambda x: x[1], reverse=True)[:5]
    message = "🏆 Top 5 Learners:\n"
    for i, (user, xp) in enumerate(sorted_users, 1):
        message += f"{i}. {user} - {xp} XP\n"

    return message.strip()

# ✅ !task
@app.route("/task")
@attendance_required
def add_task():
    username = request.args.get('user')
    userid = request.args.get('id')
    msg = request.args.get('msg')

    if not msg or len(msg.strip().split()) < 2:
        return f"⚠️ {username}, please provide a task like: !task Physics Chapter 1 or !task Studying Math."

    records = main_sheet.get_all_records()
    for row in records[::-1]:
        if str(row['UserID']) == str(userid) and str(
                row['Action']).startswith("Task:") and "✅ Done" not in str(
                    row['Action']):
            return f"⚠️ {username}, please complete your previous task first. Use `!done` to mark it as completed."

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_name = msg.strip()
    main_sheet.append_row([
        username or "", userid or "", now, f"Task: {task_name}", "0", "", "", ""
    ])
    return f"✏️ {username}, your task '{task_name}' has been added. Study well! Use `!done` to mark it as completed. Use `!remove` to remove it."

# ✅ !done
@app.route("/done")
@attendance_required
def mark_done():
    username = request.args.get('user')
    userid = request.args.get('id')

    records = main_sheet.get_all_records()

    # Calculate total minutes BEFORE this task
    previous_total_minutes = 0
    for row in records:
        if str(row['UserID']) == str(
                userid) and row['Action'] == "Study Session":
            try:
                minutes = int(str(row['Duration']).replace("min", "").strip())
                previous_total_minutes += minutes
            except (ValueError, KeyError):
                pass

    for i in range(len(records) - 1, -1, -1):
        row = records[i]
        if str(row['UserID']) == str(userid) and str(
                row['Action']).startswith("Task:") and "✅ Done" not in str(
                    row['Action']):
            row_index = i + 2
            task_name = str(row['Action'])[6:]

            # Mark task as done
            main_sheet.update_cell(row_index, 4, f"Task: {task_name} ✅ Done")

            # Add XP row for completing task
            xp_earned = 15
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            main_sheet.append_row([
                str(username),
                str(userid), now, "Task Completed",
                str(xp_earned), "", "", ""
            ])

            # Recalculate total minutes AFTER this task (no change since task isn't time-based)
            new_total_minutes = previous_total_minutes
            old_badges = get_badges(previous_total_minutes)
            new_badges = get_badges(new_total_minutes)

            badge_message = ""
            if len(new_badges) > len(old_badges):
                badge_message = f" 🎖 {username}, you unlocked a badge: {new_badges[-1]}! keep it up"

            return f"✅ {username}, you completed your task '{task_name}' and earned {xp_earned} XP! Great job! 💪{badge_message}"

    return f"⚠️ {username}, you don't have any active task. Use `!task Your Task` to add one."

# ✅ !remove
@app.route("/remove")
@attendance_required
def remove_task():
    username = request.args.get('user')
    userid = request.args.get('id')

    records = main_sheet.get_all_records()
    for i in range(len(records) - 1, -1, -1):
        row = records[i]
        if str(row['UserID']) == str(userid) and str(
                row['Action']).startswith("Task:") and "✅ Done" not in str(
                    row['Action']):
            row_index = i + 2
            task_name = str(row['Action'])[6:]
            main_sheet.delete_rows(row_index)
            return f"🗑️ {username}, your task '{task_name}' has been removed. Use `!task Your Task` to add a new one."

    return f"⚠️ {username}, you have no active task to remove. Use `!task Your Task` to add one."

# ✅ !weeklytop
@app.route("/weeklytop")
def weekly_top():
    records = main_sheet.get_all_records()
    xp_map = {}
    one_week_ago = datetime.now() - timedelta(days=7)

    for row in records:
        try:
            xp = int(row['XP'])
            timestamp = datetime.strptime(str(row['Timestamp']),
                                          "%Y-%m-%d %H:%M:%S")
            if timestamp >= one_week_ago:
                user = row['Username']
                xp_map[user] = xp_map.get(user, 0) + xp
        except (ValueError, KeyError):
            continue

    sorted_users = sorted(xp_map.items(), key=lambda x: x[1], reverse=True)[:5]
    message = "📆 Weekly Top 5 Learners:\n"
    for i, (user, xp) in enumerate(sorted_users, 1):
        message += f"{i}. {user} - {xp} XP\n"

    return message.strip()

# ✅ !goal
@app.route("/goal")
@attendance_required
def goal():
    username = request.args.get('user')
    userid = request.args.get('id')
    msg = request.args.get('msg') or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    records = main_sheet.get_all_records()
    user_row_index = None

    # Find the last goal row or existing goal row for the user
    for i in range(len(records) - 1, -1, -1):
        row = records[i]
        if str(row['UserID']) == str(userid) and row.get('Goal'):
            user_row_index = i + 2
            break

    if msg.strip():
        # Set or update goal
        if user_row_index:
            main_sheet.update_cell(user_row_index, 9,
                              msg.strip())  # Update Goal column
        else:
            main_sheet.append_row([
                username or "", userid or "", now, "Set Goal", "0", "", "", "",
                msg.strip()
            ])
        return f"🎯 {username}, your goal has been set to: {msg.strip()} Use `!complete` to mark it as achieved. Use `!goal` to view your current goal."
    else:
        # Show existing goal
        for row in records[::-1]:
            if str(row['UserID']) == str(userid) and row.get('Goal'):
                return f"🎯 {username}, your current goal is: {row['Goal']} Use `!complete` to mark it as achieved."
        return f"⚠️ {username}, you haven't set any goal. Use `!goal Your Goal` to set one."

# ✅ !complete
@app.route("/complete")
@attendance_required
def complete_goal():
    username = request.args.get('user')
    userid = request.args.get('id')

    records = main_sheet.get_all_records()

    for i in range(len(records) - 1, -1, -1):
        row = records[i]
        if str(row['UserID']) == str(userid) and row.get('Goal'):
            row_index = i + 2
            main_sheet.update_cell(row_index, 9, "")  # Clear the goal
            return f"🎉 {username}, you achieved your goal! Congratulations!"

    return f"⚠️ {username}, you don't have any goal set. Use `!goal Your Goal` to set one."

# ✅ !summary
@app.route("/summary")
@attendance_required
def summary():
    username = request.args.get('user')
    userid = request.args.get('id')

    records = main_sheet.get_all_records()

    total_minutes = 0
    total_xp = 0
    completed_tasks = 0
    pending_tasks = 0

    for row in records:
        if str(row['UserID']) == str(userid):
            # Total XP
            try:
                total_xp += int(row['XP'])
            except ValueError:
                pass

            # Study time
            if row['Action'] == "Study Session":
                duration_str = str(row.get('Duration',
                                           '0')).replace(" min", "")
                try:
                    total_minutes += int(duration_str)
                except ValueError:
                    pass

            # Tasks
            if str(row['Action']).startswith("Task:"):
                if "✅ Done" in str(row['Action']):
                    completed_tasks += 1
                else:
                    pending_tasks += 1

    hours = total_minutes // 60
    minutes = total_minutes % 60
    return (f"📊 {username}'s Summary:\n"
            f"⏱️ Total Study Time: {hours}h {minutes}m\n"
            f"⚜️ Total XP: {total_xp}\n"
            f"✅ Completed Tasks: {completed_tasks}\n"
            f"🕒 Pending Tasks: {pending_tasks}")

# ✅ !pending
@app.route("/pending")
@attendance_required
def pending_task():
    username = request.args.get('user')
    userid = request.args.get('id')

    records = main_sheet.get_all_records()

    for row in reversed(records):
        if str(row['UserID']) == str(userid) and str(
                row['Action']).startswith("Task:") and "✅ Done" not in str(
                    row['Action']):
            task_name = str(row['Action'])[6:]  # Remove "Task: " prefix
            return f"🕒 {username}, your current pending task is: '{task_name}' — Keep going. Use `!done` to mark it as completed. Use `!remove` to remove it."

    return f"✅ {username}, you have no pending tasks! Use `!task Your Task` to add one."

# === New Commands ===

# !break
@app.route("/break")
@attendance_required
def take_break():
    username = request.args.get('user')
    userid = request.args.get('id')
    duration = request.args.get('msg') or "5 min"
    
    try:
        break_time = parse_time_input(duration)
        reminders_sheet.append_row([
            userid, username, 
            break_time.strftime("%Y-%m-%d %H:%M:%S"),
            "Break time is over! Get back to study!",
            "FALSE",
            duration
        ])
        return f"☕ {username}, break started! I'll remind you at {break_time.strftime('%H:%M')}."
    except Exception:
        return f"⚠️ {username}, invalid time format. Use like: !break 5 min"

# !remind
@app.route("/remind")
@attendance_required
def set_reminder():
    username = request.args.get('user')
    userid = request.args.get('id')
    msg = request.args.get('msg') or ""
    
    if not msg:
        return f"⚠️ {username}, please specify reminder time and message. Example: !remind 30 min finish math assignment"
    
    try:
        # Extract time and message
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            return f"⚠️ {username}, include both time and message. Example: !remind 30 min finish math"
            
        time_part, reminder_msg = parts
        reminder_time = parse_time_input(time_part)
        
        reminders_sheet.append_row([
            userid, username, 
            reminder_time.strftime("%Y-%m-%d %H:%M:%S"),
            reminder_msg,
            "FALSE",
            time_part
        ])
        return f"⏰ {username}, reminder set for {reminder_time.strftime('%H:%M')}: {reminder_msg}"
    except Exception:
        return f"⚠️ {username}, invalid reminder format. Use: !remind [time] [message]"

# !comtask
@app.route("/comtask")
@attendance_required
def completed_tasks():
    username = request.args.get('user')
    userid = request.args.get('id')
    
    records = main_sheet.get_all_records()
    completed = []
    
    for row in records:
        if (str(row['UserID']) == str(userid) and 
            str(row['Action']).startswith("Task:") and 
            "✅ Done" in str(row['Action'])):
            task_name = str(row['Action']).split("Task: ")[1].replace("✅ Done", "").strip()
            completed.append(task_name)
    
    if not completed:
        return f"📭 {username}, you haven't completed any tasks yet."
    
    # Get last 3 completed tasks
    last_three = completed[-3:]
    response = f"✅ {username}'s recently completed tasks:\n"
    for i, task in enumerate(reversed(last_three), 1):
        response += f"{i}. {task}\n"
    
    return response.strip()

# !monthtop
@app.route("/monthtop")
def monthly_leaderboard():
    records = main_sheet.get_all_records()
    xp_map = {}
    now = datetime.now()
    
    for row in records:
        try:
            timestamp = datetime.strptime(str(row['Timestamp']), "%Y-%m-%d %H:%M:%S")
            if timestamp.month == now.month and timestamp.year == now.year:
                user = row['Username']
                xp = int(row['XP'])
                xp_map[user] = xp_map.get(user, 0) + xp
        except (ValueError, KeyError):
            continue
    
    sorted_users = sorted(xp_map.items(), key=lambda x: x[1], reverse=True)[:5]
    message = "📅 Monthly Top 5 Learners:\n"
    for i, (user, xp) in enumerate(sorted_users, 1):
        message += f"{i}. {user} - {xp} XP\n"
    
    return message.strip()

# !report
@app.route("/report")
@attendance_required
def report_user():
    reporter = request.args.get('user')
    userid = request.args.get('id')
    msg = request.args.get('msg') or ""
    
    if not msg or len(msg.split()) < 2:
        return f"⚠️ {reporter}, please specify user and reason. Example: !report username Spamming chat"
    
    reported_user, *reason_parts = msg.split(maxsplit=1)
    reason = reason_parts[0] if reason_parts else "No reason provided"
    
    reports_sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        reporter,
        reported_user,
        reason,
        "FALSE"
    ])
    
    return f"📢 {reporter}, your report against {reported_user} has been recorded. Moderators will review it."

# !plan
@app.route("/plan")
@attendance_required
def study_plan():
    username = request.args.get('user')
    userid = request.args.get('id')
    msg = request.args.get('msg') or ""
    
    if not msg:
        return f"⚠️ {username}, please describe your planning needs. Example: !plan exam in 3 days covering physics and math"
    
    prompt = f"Create a concise study plan for: {msg}. Max 200 characters."
    plan = query_ai(prompt)
    return f"📚 {username}, here's your study plan:\n{plan}"

# !progress
@app.route("/progress")
@attendance_required
def progress_report():
    username = request.args.get('user')
    userid = request.args.get('id')
    period = request.args.get('msg') or "overall"
    
    # Get user's study data
    records = main_sheet.get_all_records()
    study_data = []
    
    for row in records:
        if str(row['UserID']) == str(userid) and row['Action'] == "Study Session":
            try:
                duration = int(str(row['Duration']).replace("min", "").strip())
                study_data.append({
                    "date": row['Timestamp'],
                    "duration": duration
                })
            except:
                pass
    
    # Create prompt for AI
    prompt = f"Generate a progress report and suggestions for {username} based on their {period} study data: {study_data}. Max 200 characters."
    report = query_ai(prompt)
    return f"📊 {username}, your progress report:\n{report}"

# !ai
@app.route("/ai")
@attendance_required
def ai_assistant():
    username = request.args.get('user')
    question = request.args.get('msg') or ""
    
    if not question:
        return f"⚠️ {username}, ask like: !ai explain quantum physics"
    
    if len(question) < 5:
        return f"📝 {username}, please ask a longer question (min 5 chars)"
        
    response = query_ai(question)
    
    # Truncate to chat limits
    if len(response) > 200:
        response = response[:197] + "..."
        
    return f"🤖 {username}: {response}"

# !working
@app.route("/working")
@attendance_required
def confirm_working():
    username = request.args.get('user')
    userid = request.args.get('id')
    
    session = get_user_active_session(userid)
    if not session:
        return f"⚠️ {username}, you don't have an active study session."
    
    # Update last active time
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row_idx = sessions_sheet.find(session['UserID']).row
    sessions_sheet.update_cell(row_idx, 4, now)
    sessions_sheet.update_cell(row_idx, 5, "active")
    
    return f"👩‍💻 {username}, activity confirmed! Keep up the good work!"

# Health checks
@app.route("/ping")
def home():
    return "✅ Sunnie-BOT is alive!"

# === Run Server ===
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
