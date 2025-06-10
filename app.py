from flask import Flask, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import time
from threading import Thread
import os

app = Flask(__name__)

# === Google Sheet Setup ===
SERVICE_ACCOUNT_FILE = "/etc/secrets/credentials.json"
scope = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]
client = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
spreadsheet = client.open("StudyPlusData")

# Define separate sheets
attendance_sheet = spreadsheet.worksheet("attendance")
session_sheet = spreadsheet.worksheet("session") 
task_sheet = spreadsheet.worksheet("task")
xp_sheet = spreadsheet.worksheet("xp")

# === Helper Functions ===
def update_user_xp(username, userid, xp_earned, action_type):
    """Update or create user XP record in the xp sheet"""
    try:
        records = xp_sheet.get_all_records()
        user_found = False
        
        for i, row in enumerate(records):
            if str(row['UserID']) == str(userid):
                # Update existing user
                current_xp = int(row.get('TotalXP', 0))
                new_total = current_xp + int(xp_earned)
                xp_sheet.update_cell(i + 2, 3, new_total)  # TotalXP column
                xp_sheet.update_cell(i + 2, 4, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))  # LastUpdated
                user_found = True
                break
        
        if not user_found:
            # Add new user
            xp_sheet.append_row([
                username,
                userid,
                int(xp_earned),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ])
    except Exception as e:
        print(f"Error updating XP: {e}")


def get_user_total_xp(userid):
    """Get user's total XP from xp sheet"""
    try:
        records = xp_sheet.get_all_records()
        for row in records:
            if str(row['UserID']) == str(userid):
                return int(row.get('TotalXP', 0))
        return 0
    except:
        return 0


def calculate_streak(userid):
    """Calculate daily streak from attendance sheet"""
    try:
        records = attendance_sheet.get_all_records()
        dates = set()
        for row in records:
            if str(row['UserID']) == str(userid):
                try:
                    date = datetime.strptime(str(row['Date']), "%Y-%m-%d %H:%M:%S").date()
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


# === ROUTES ===

# ✅ !attend
@app.route("/attend")
def attend():
    username = request.args.get('user') or ""
    userid = request.args.get('id') or ""
    now = datetime.now()
    today_date = now.date()

    # Check if this user already gave attendance today
    try:
        records = attendance_sheet.get_all_records()
        for row in records[::-1]:
            if str(row['UserID']) == str(userid):
                try:
                    row_date = datetime.strptime(str(row['Date']), "%Y-%m-%d %H:%M:%S").date()
                    if row_date == today_date:
                        return f"⚠️ {username}, your attendance for today is already recorded! ✅"
                except ValueError:
                    continue
    except:
        pass

    # Log new attendance
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    attendance_sheet.append_row([username, userid, timestamp])
    
    # Update XP
    update_user_xp(username, userid, 10, "Attendance")
    
    streak = calculate_streak(userid)
    return f"✅ {username}, your attendance is logged and you earned 10 XP! 🔥 Daily Streak: {streak} days."


@app.route("/start")
def start():
    username = request.args.get('user')
    userid = request.args.get('id')
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        records = session_sheet.get_all_records()
        # Check if a session is already running (not marked as completed)
        for row in reversed(records):
            if str(row.get('UserID', '')) == str(userid) and str(row.get('Status', '')).strip() == 'Active':
                return f"⚠️ {username}, you already started a session. Use `!stop` before starting a new one."
    except Exception as e:
        print(f"Error checking sessions: {e}")

    # Log new session start
    session_sheet.append_row([username, userid, now, "", "", "Active"])
    return f"⏱️ {username}, your study session has started! Use `!stop` to end it. Happy studying 📚"


# ✅ !stop
@app.route("/stop")
def stop():
    username = request.args.get('user')
    userid = request.args.get('id')
    now = datetime.now()

    try:
        records = session_sheet.get_all_records()
        
        # Find the latest active session
        session_start = None
        row_index = None
        for i in range(len(records) - 1, -1, -1):
            row = records[i]
            if (str(row.get('UserID', '')) == str(userid) and str(row.get('Status', '')).strip() == 'Active'):
                try:
                    session_start = datetime.strptime(row.get('StartTime', ''), "%Y-%m-%d %H:%M:%S")
                    row_index = i + 2
                    break
                except (ValueError, TypeError):
                    print(f"Error parsing start time: {row.get('StartTime', '')}")
                    continue

        if not session_start:
            return f"⚠️ {username}, you didn't start any session. Use `!start` to begin."

        # Calculate duration and XP
        duration_minutes = int((now - session_start).total_seconds() / 60)
        xp_earned = duration_minutes * 2

        # Update the session record
        session_sheet.update_cell(row_index, 4, now.strftime("%Y-%m-%d %H:%M:%S"))  # EndTime
        session_sheet.update_cell(row_index, 5, duration_minutes)  # Duration
        session_sheet.update_cell(row_index, 6, "Completed")  # Status

        # Update XP
        update_user_xp(username, userid, xp_earned, "Study Session")

        # Badge check
        badges = get_badges(duration_minutes)
        badge_message = f" 🎖 {username}, you unlocked a badge: {badges[-1]}! Keep it up!" if badges else ""

        return f"👩🏻‍💻📓✍🏻 {username}, you studied for {duration_minutes} minutes and earned {xp_earned} XP.{badge_message}"
    
    except Exception as e:
        return f"⚠️ Error stopping session: {str(e)}"


# ✅ !rank
@app.route("/rank")
def rank():
    username = request.args.get('user')
    userid = request.args.get('id')

    total_xp = get_user_total_xp(userid)
    user_rank = get_rank(total_xp)
    return f"🏅 {username}, you have {total_xp} XP. Your rank is: {user_rank}"


# ✅ !top
@app.route("/top")
def leaderboard():
    try:
        records = xp_sheet.get_all_records()
        sorted_users = sorted(records, key=lambda x: int(x.get('TotalXP', 0)), reverse=True)[:5]
        
        message = "🏆 Top 5 Learners:\n"
        for i, user in enumerate(sorted_users, 1):
            message += f"{i}. {user['Username']} - {user.get('TotalXP', 0)} XP\n"

        return message.strip()
    except:
        return "⚠️ Unable to fetch leaderboard data."


# ✅ !task
@app.route("/task")
def add_task():
    username = request.args.get('user')
    userid = request.args.get('id')
    msg = request.args.get('msg')

    if not msg or len(msg.strip().split()) < 2:
        return f"⚠️ {username}, please provide a task like: !task Physics Chapter 1 or !task Studying Math."

    try:
        records = task_sheet.get_all_records()
        for row in records[::-1]:
            if str(row.get('UserID', '')) == str(userid) and str(row.get('Status', '')).strip() == 'Pending':
                return f"⚠️ {username}, please complete your previous task first. Use `!done` to mark it as completed."
    except Exception as e:
        print(f"Error checking tasks: {e}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_name = msg.strip()
    task_sheet.append_row([username, userid, task_name, now, "", "Pending"])
    return f"✏️ {username}, your task '{task_name}' has been added. Study well! Use `!done` to mark it as completed. Use `!remove` to remove it."


# ✅ !done
@app.route("/done")
def mark_done():
    username = request.args.get('user')
    userid = request.args.get('id')

    try:
        records = task_sheet.get_all_records()

        for i in range(len(records) - 1, -1, -1):
            row = records[i]
            if str(row.get('UserID', '')) == str(userid) and str(row.get('Status', '')).strip() == 'Pending':
                row_index = i + 2
                task_name = row.get('TaskName', '')

                # Mark task as completed
                task_sheet.update_cell(row_index, 5, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))  # CompletedDate
                task_sheet.update_cell(row_index, 6, "Completed")  # Status

                # Update XP
                xp_earned = 15
                update_user_xp(username, userid, xp_earned, "Task Completed")

                return f"✅ {username}, you completed your task '{task_name}' and earned {xp_earned} XP! Great job! 💪"

        return f"⚠️ {username}, you don't have any active task. Use `!task Your Task` to add one."
    except Exception as e:
        return f"⚠️ Error completing task: {str(e)}"


# ✅ !remove
@app.route("/remove")
def remove_task():
    username = request.args.get('user')
    userid = request.args.get('id')

    try:
        records = task_sheet.get_all_records()
        for i in range(len(records) - 1, -1, -1):
            row = records[i]
            if str(row.get('UserID', '')) == str(userid) and str(row.get('Status', '')).strip() == 'Pending':
                row_index = i + 2
                task_name = row.get('TaskName', '')
                task_sheet.delete_rows(row_index)
                return f"🗑️ {username}, your task '{task_name}' has been removed. Use `!task Your Task` to add a new one."

        return f"⚠️ {username}, you have no active task to remove. Use `!task Your Task` to add one."
    except Exception as e:
        return f"⚠️ Error removing task: {str(e)}"


# ✅ !weeklytop
@app.route("/weeklytop")
def weekly_top():
    try:
        records = xp_sheet.get_all_records()
        one_week_ago = datetime.now() - timedelta(days=7)
        
        weekly_xp = {}
        for user in records:
            try:
                last_updated = datetime.strptime(user['LastUpdated'], "%Y-%m-%d %H:%M:%S")
                if last_updated >= one_week_ago:
                    weekly_xp[user['Username']] = int(user.get('TotalXP', 0))
            except:
                continue

        sorted_users = sorted(weekly_xp.items(), key=lambda x: x[1], reverse=True)[:5]
        message = "📆 Weekly Top 5 Learners:\n"
        for i, (user, xp) in enumerate(sorted_users, 1):
            message += f"{i}. {user} - {xp} XP\n"

        return message.strip()
    except:
        return "⚠️ Unable to fetch weekly leaderboard data."


# ✅ !summary
@app.route("/summary")
def summary():
    username = request.args.get('user')
    userid = request.args.get('id')

    try:
        # Get total XP
        total_xp = get_user_total_xp(userid)
        
        # Get total study time from sessions
        session_records = session_sheet.get_all_records()
        total_minutes = 0
        for row in session_records:
            if str(row['UserID']) == str(userid) and row['Status'] == 'Completed':
                try:
                    total_minutes += int(row['Duration'])
                except ValueError:
                    pass

        # Get task counts
        task_records = task_sheet.get_all_records()
        completed_tasks = 0
        pending_tasks = 0
        for row in task_records:
            if str(row['UserID']) == str(userid):
                if row['Status'] == 'Completed':
                    completed_tasks += 1
                elif row['Status'] == 'Pending':
                    pending_tasks += 1

        hours = total_minutes // 60
        minutes = total_minutes % 60
        return (f"📊 {username}'s Summary:\n"
                f"⏱️ Total Study Time: {hours}h {minutes}m\n"
                f"⚜️ Total XP: {total_xp}\n"
                f"✅ Completed Tasks: {completed_tasks}\n"
                f"🕒 Pending Tasks: {pending_tasks}")
    except Exception as e:
        return f"⚠️ Error generating summary: {str(e)}"


# ✅ !pending
@app.route("/pending")
def pending_task():
    username = request.args.get('user')
    userid = request.args.get('id')

    try:
        records = task_sheet.get_all_records()
        for row in reversed(records):
            if str(row.get('UserID', '')) == str(userid) and str(row.get('Status', '')).strip() == 'Pending':
                task_name = row.get('TaskName', '')
                return f"🕒 {username}, your current pending task is: '{task_name}' — Keep going. Use `!done` to mark it as completed. Use `!remove` to remove it."

        return f"✅ {username}, you have no pending tasks! Use `!task Your Task` to add one."
    except Exception as e:
        return f"⚠️ Error fetching pending tasks: {str(e)}"


# ✅ !comtask
@app.route("/comtask")
def completed_tasks():
    username = request.args.get('user')
    userid = request.args.get('id')

    try:
        records = task_sheet.get_all_records()
        completed = []

        for row in reversed(records):
            if str(row.get('UserID', '')) == str(userid) and str(row.get('Status', '')).strip() == 'Completed':
                completed.append(row.get('TaskName', ''))
                if len(completed) == 3:
                    break

        if not completed:
            return f"📭 {username}, you haven't completed any tasks yet. Use `!task` to add one."

        task_list = "\n".join([f"{i+1}. {task}" for i, task in enumerate(completed)])
        return f"✅ {username}, here are your last 3 completed tasks:\n{task_list}"
    except Exception as e:
        return f"⚠️ Error fetching completed tasks: {str(e)}"


# Goal functionality (you can add a separate goal sheet if needed)
@app.route("/goal")
def goal():
    username = request.args.get('user')
    userid = request.args.get('id')
    msg = request.args.get('msg') or ""
    
    # For now, using task sheet with a special goal task type
    # You can create a separate goal sheet if needed
    return f"🎯 Goal functionality - implement with separate goal sheet if needed"


@app.route("/complete")
def complete_goal():
    username = request.args.get('user')
    userid = request.args.get('id')
    return f"🎉 Goal completion - implement with separate goal sheet if needed"


@app.route("/ping")
def ping():
    return "🟢 Sunnie-BOT is alive!"


# === Run Server ===
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
