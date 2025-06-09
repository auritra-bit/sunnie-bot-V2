from flask import Flask, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import time
from threading import Thread, Lock
import os
from functools import lru_cache
import concurrent.futures
import logging

# Initialize logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# === Google Sheet Setup ===
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
scope = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]
try:
    client = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sheet = client.open("StudyPlusData").sheet1
    logger.info("Connected to Google Sheets successfully")
except Exception as e:
    logger.error(f"Failed to connect to Google Sheets: {str(e)}")
    raise

# === Caching System ===
CACHE_TTL = 60  # 1 minute cache
cache_lock = Lock()
cached_records = []
last_updated = 0
user_cache = {}  # Separate cache for user-specific data

# Thread pool for async writes
executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

def refresh_cache():
    """Refresh the cache with latest data from Google Sheets"""
    global cached_records, last_updated, user_cache
    start_time = time.time()
    
    with cache_lock:
        try:
            cached_records = sheet.get_all_records()
            last_updated = time.time()
            # Clear user-specific cache
            user_cache = {}
            logger.info(f"Cache refreshed in {time.time()-start_time:.2f}s")
            return True
        except Exception as e:
            logger.error(f"Cache refresh failed: {str(e)}")
            return False

def get_cached_records():
    """Get cached records, refresh if stale"""
    global last_updated
    current_time = time.time()
    
    # If cache is empty or expired
    if current_time - last_updated > CACHE_TTL or not cached_records:
        logger.info("Cache stale, refreshing...")
        if not refresh_cache():
            # Return empty list if refresh fails
            return []
    
    return cached_records

def get_user_records(userid):
    """Get records for specific user with caching"""
    # Check if we have cached user records
    if userid in user_cache:
        return user_cache[userid]
    
    records = get_cached_records()
    user_records = [r for r in records if str(r.get('UserID', '')) == str(userid)]
    
    # Cache user records
    user_cache[userid] = user_records
    return user_records

def invalidate_user_cache(userid):
    """Invalidate cache for specific user"""
    if userid in user_cache:
        del user_cache[userid]
        logger.info(f"Invalidated cache for user {userid}")

def async_append_row(row_data):
    """Append row asynchronously and refresh cache"""
    try:
        sheet.append_row(row_data)
        logger.info(f"Appended row: {row_data[:3]}...")
        refresh_cache()
        return True
    except Exception as e:
        logger.error(f"Async append failed: {str(e)}")
        return False

def async_update_cell(row, col, value):
    """Update cell asynchronously and refresh cache"""
    try:
        sheet.update_cell(row, col, value)
        logger.info(f"Updated cell ({row},{col}) = {value}")
        refresh_cache()
        return True
    except Exception as e:
        logger.error(f"Async update failed: {str(e)}")
        return False

# === Rank System ===
@lru_cache(maxsize=512)
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
@lru_cache(maxsize=512)
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
@lru_cache(maxsize=512)
def calculate_streak(userid):
    user_records = get_user_records(userid)
    dates = set()
    
    for row in user_records:
        if row.get('Action') == 'Attendance':
            try:
                date_str = str(row.get('Timestamp', ''))
                if len(date_str) >= 10:  # Minimum valid date length
                    date = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S").date()
                    dates.add(date)
            except (ValueError, TypeError):
                continue

    if not dates:
        return 0

    streak = 0
    today = datetime.now().date()
    current_date = today

    # Check consecutive days from today backwards
    while current_date in dates:
        streak += 1
        current_date -= timedelta(days=1)
        
    return streak

# === ROUTES ===
@app.route("/attend")
def attend():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""
    today_date = datetime.now().date()

    # Check existing attendance
    user_records = get_user_records(userid)
    for row in reversed(user_records):
        if row.get('Action') == 'Attendance':
            try:
                ts_str = str(row.get('Timestamp', ''))
                if len(ts_str) >= 10:
                    row_date = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S").date()
                    if row_date == today_date:
                        return f"⚠️ {username}, attendance already recorded! ✅"
            except (ValueError, TypeError):
                continue

    # Log new attendance
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    executor.submit(
        async_append_row,
        [username, userid, timestamp, "Attendance", "10", "", "", ""]
    )
    invalidate_user_cache(userid)  # Invalidate user cache
    
    streak = calculate_streak(userid)
    return f"✅ {username}, attendance logged +10 XP! 🔥 Streak: {streak} days."

@app.route("/start")
def start():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Check existing sessions
    user_records = get_user_records(userid)
    for row in reversed(user_records):
        if row.get('Action') == 'Session Start':
            return f"⚠️ {username}, session already started. Use `!stop` first."

    # Start new session
    executor.submit(
        async_append_row,
        [username, userid, now, "Session Start", "0", "", "", ""]
    )
    invalidate_user_cache(userid)  # Invalidate user cache
    return f"⏱️ {username}, study session started! Use `!stop` to end."

@app.route("/stop")
def stop():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""
    now = datetime.now()

    # Find latest session start
    user_records = get_user_records(userid)
    session_start = None
    row_index = None
    
    for i, row in enumerate(reversed(user_records)):
        if row.get('Action') == 'Session Start':
            try:
                ts_str = str(row.get('Timestamp', ''))
                if len(ts_str) >= 19:
                    session_start = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
                    # Calculate original row index (reverse index math)
                    row_index = len(user_records) - i
                    break
            except (ValueError, TypeError):
                continue

    if not session_start:
        return f"⚠️ {username}, no active session found."

    # Calculate duration and XP
    duration_minutes = max(1, int((now - session_start).total_seconds() / 60))
    xp_earned = duration_minutes * 2

    # Log session
    session_data = [
        username, userid,
        now.strftime("%Y-%m-%d %H:%M:%S"),
        "Study Session", str(xp_earned),
        session_start.strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%Y-%m-%d %H:%M:%S"),
        f"{duration_minutes} min"
    ]
    executor.submit(async_append_row, session_data)

    # Mark session as completed
    executor.submit(async_update_cell, row_index + 1, 4, "Session Start ✅")
    invalidate_user_cache(userid)  # Invalidate user cache

    # Check badges
    badges = get_badges(duration_minutes)
    badge_msg = f" 🎖 {badges[-1]}!" if badges else ""
    
    return f"👩🏻‍💻 {username}, studied {duration_minutes} min, earned {xp_earned} XP.{badge_msg}"

@app.route("/rank")
def rank():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""

    user_records = get_user_records(userid)
    total_xp = 0

    for row in user_records:
        try:
            total_xp += int(row.get('XP', 0))
        except (ValueError, TypeError):
            continue

    user_rank = get_rank(total_xp)
    return f"🏅 {username}, {total_xp} XP. Rank: {user_rank}"

@app.route("/top")
def leaderboard():
    records = get_cached_records()
    xp_map = {}

    for row in records:
        try:
            name = row.get('Username', 'Unknown')
            xp = int(row.get('XP', 0))
            if name and xp > 0:
                xp_map[name] = xp_map.get(name, 0) + xp
        except (ValueError, TypeError):
            continue

    sorted_users = sorted(xp_map.items(), key=lambda x: x[1], reverse=True)[:5]
    message = "🏆 Top 5 Learners:\n"
    for i, (user, xp) in enumerate(sorted_users, 1):
        message += f"{i}. {user} - {xp} XP\n"

    return message.strip()

@app.route("/task")
def add_task():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""
    msg = request.args.get('msg') or ""

    if not msg or len(msg.strip().split()) < 2:
        return f"⚠️ {username}, invalid task format."

    # Check existing tasks
    user_records = get_user_records(userid)
    for row in reversed(user_records):
        action = row.get('Action', '')
        if action.startswith("Task:") and "✅ Done" not in action:
            return f"⚠️ {username}, complete previous task first."

    # Add new task
    task_name = msg.strip()
    executor.submit(
        async_append_row,
        [username, userid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
         f"Task: {task_name}", "0", "", "", ""]
    )
    invalidate_user_cache(userid)  # Invalidate user cache
    return f"✏️ {username}, task added: '{task_name}'"

@app.route("/done")
def mark_done():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""

    # Find latest active task
    user_records = get_user_records(userid)
    for i, row in enumerate(reversed(user_records)):
        action = row.get('Action', '')
        if action.startswith("Task:") and "✅ Done" not in action:
            task_name = action[6:]
            row_index = len(user_records) - i
            
            # Mark as done
            executor.submit(
                async_update_cell, 
                row_index + 1, 4, 
                f"Task: {task_name} ✅ Done"
            )
            
            # Add XP
            executor.submit(
                async_append_row,
                [username, userid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "Task Completed", "15", "", "", ""]
            )
            
            # Invalidate user cache immediately
            invalidate_user_cache(userid)
            
            return f"✅ {username}, task completed! +15 XP"

    return f"⚠️ {username}, no active tasks found."

@app.route("/remove")
def remove_task():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""

    # Find latest active task
    user_records = get_user_records(userid)
    for i, row in enumerate(reversed(user_records)):
        action = row.get('Action', '')
        if action.startswith("Task:") and "✅ Done" not in action:
            row_index = len(user_records) - i
            task_name = action[6:]
            
            try:
                sheet.delete_rows(row_index + 1)
                refresh_cache()
                invalidate_user_cache(userid)
                return f"🗑️ {username}, task removed."
            except Exception as e:
                logger.error(f"Task deletion failed: {str(e)}")
                return f"⚠️ {username}, task removal failed."

    return f"⚠️ {username}, no active tasks to remove."

@app.route("/weeklytop")
def weekly_top():
    records = get_cached_records()
    xp_map = {}
    one_week_ago = datetime.now() - timedelta(days=7)

    for row in records:
        try:
            ts_str = str(row.get('Timestamp', ''))
            if len(ts_str) >= 19:
                timestamp = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
                if timestamp >= one_week_ago:
                    name = row.get('Username', 'Unknown')
                    xp = int(row.get('XP', 0))
                    if name and xp > 0:
                        xp_map[name] = xp_map.get(name, 0) + xp
        except (ValueError, TypeError):
            continue

    sorted_users = sorted(xp_map.items(), key=lambda x: x[1], reverse=True)[:5]
    message = "📆 Weekly Top 5:\n"
    for i, (user, xp) in enumerate(sorted_users, 1):
        message += f"{i}. {user} - {xp} XP\n"

    return message.strip()

@app.route("/goal")
def goal():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""
    msg = request.args.get('msg') or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    user_records = get_user_records(userid)
    
    if msg.strip():
        # Set new goal
        executor.submit(
            async_append_row,
            [username, userid, now, "Set Goal", "0", "", "", "", msg.strip()]
        )
        invalidate_user_cache(userid)
        return f"🎯 {username}, goal set: {msg.strip()}"
    else:
        # Show existing goal
        for row in reversed(user_records):
            if row.get('Goal'):
                return f"🎯 {username}, current goal: {row['Goal']}"
        return f"⚠️ {username}, no goal set."

@app.route("/complete")
def complete_goal():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""

    user_records = get_user_records(userid)
    for i, row in enumerate(reversed(user_records)):
        if row.get('Goal'):
            row_index = len(user_records) - i
            executor.submit(
                async_update_cell, 
                row_index + 1, 9, 
                ""
            )
            invalidate_user_cache(userid)
            return f"🎉 {username}, goal achieved!"

    return f"⚠️ {username}, no active goal."

@app.route("/summary")
def summary():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""

    user_records = get_user_records(userid)
    total_minutes = 0
    total_xp = 0
    completed_tasks = 0
    pending_tasks = 0

    for row in user_records:
        # XP calculation
        try:
            total_xp += int(row.get('XP', 0))
        except (ValueError, TypeError):
            pass
        
        # Study time
        if row.get('Action') == "Study Session":
            try:
                duration = str(row.get('Duration', '0')).replace("min", "").strip()
                total_minutes += int(duration) if duration.isdigit() else 0
            except (ValueError, TypeError):
                pass
        
        # Task counts
        action = row.get('Action', '')
        if action.startswith("Task:"):
            if "✅ Done" in action:
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

@app.route("/pending")
def pending_task():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""

    user_records = get_user_records(userid)
    for row in reversed(user_records):
        action = row.get('Action', '')
        if action.startswith("Task:") and "✅ Done" not in action:
            return f"🕒 {username}, current task: '{action[6:]}'"

    return f"✅ {username}, no pending tasks!"

@app.route("/comtask")
def completed_tasks():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""

    user_records = get_user_records(userid)
    completed = []
    
    for row in reversed(user_records):
        action = row.get('Action', '')
        if action.startswith("Task:") and "✅ Done" in action:
            task_name = action[6:].replace("✅ Done", "").strip()
            completed.append(task_name)
            if len(completed) >= 3:
                break

    if not completed:
        return f"📭 {username}, no completed tasks."

    task_list = "\n".join([f"{i+1}. {task}" for i, task in enumerate(completed)])
    return f"✅ {username}'s completed tasks:\n{task_list}"

@app.route("/ping")
def ping():
    try:
        # Quick sheet access test
        sheet.row_count
        return "🟢 Server & Sheets Connected"
    except Exception as e:
        return f"🔴 Connection Error: {str(e)}"

# === Initialization ===
if __name__ == "__main__":
    # Initial cache load
    refresh_cache()
    app.run(host="0.0.0.0", port=8080, threaded=True)
