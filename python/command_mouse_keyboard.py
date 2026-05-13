#!/usr/bin/env python3
# ==============================================================================
# command_mouse_keyboard.py – Version 39.15.1
#   - Log file writes are atomic and never crash the agent
#   - Screenshot worker is monitored and auto‑restarted on failure
#   - Profile cache split into 45 MB chunks (GitHub‑safe)
# ==============================================================================
import os, time, subprocess, hashlib, sys, base64, json, random, threading, traceback, io, shutil, tarfile, glob, re
from datetime import datetime, timezone
from pyvirtualdisplay import Display
from cryptography.fernet import Fernet, InvalidToken
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.chrome.options import Options
from PIL import Image, ImageDraw

from crypto_utils import encrypt_string, decode_string
from comments import (
    get_all_comments, find_marker_comment, issue_comment,
    delete_comment, edit_comment, comment_exists, gh_api as gh
)
from uploader import reassemble as upload_reassemble
from execution_queue import ExecutionQueue
from command_handlers import execute_one_command

from agent_state import (
    log as default_log,
    driver as state_driver, W as state_W, H as state_H,
    cursor_x as state_cx, cursor_y as state_cy,
    HAS_GEMINI, HAS_PYPERCLIP, pyperclip, allowed_secrets,
    move_cursor_absolute, move_cursor_relative,
    left_click, left_button_down, left_button_up,
    right_button_down, right_button_up,
    middle_button_down, middle_button_up,
    double_click, right_click, middle_click,
    scroll_by, drag_from_to,
    press_key, press_combo, type_secret,
    parse_single_command,
    refresh_file_registry, get_upload_paths,
    refresh_known_handles,
    url_monitor_worker,
    add_autonomous_report, cull_expired_autonomous_reports,
    pending_autonomous_reports,
    _file_registry, _upload_file_paths,
    _known_handles, _last_known_url, _url_monitor_stop,
    autonomous_counter,
    KEY_MAP, human_click, human_click_at,
    _perform_human_click_at, _try_gemini_click
)

# ---------- Logging – robust file writer (never crashes) ----------
LOG_FILENAME = "logs/command_mouse_keyboard.log"
os.makedirs("logs", exist_ok=True)

_logfile = open(LOG_FILENAME, "a", encoding="utf-8")  # append mode for resilience
_log_lock = threading.Lock()          # ensure writes from multiple threads are safe
_log_closed = False

def safe_log_write(message: str) -> None:
    """Write a message to the log file and flush. Swallows all errors."""
    global _log_closed
    if _log_closed:
        return
    try:
        with _log_lock:
            _logfile.write(message + "\n")
            _logfile.flush()
    except Exception:
        pass

def echo(msg: str) -> None:
    # Print to original stdout (still visible in workflow output) AND write to log file
    print(msg, flush=True)
    safe_log_write(msg)

def log(msg: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    echo(f"[{now}] {msg}")

echo(f"{'='*60}\n  Remote Control v39.15.1 started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n{'='*60}")
os.makedirs("screenshots", exist_ok=True)

COMM_INTERVAL = 5.0
slow_mode = 1
last_command_time = time.time()

# ---------- Global git lock ----------
_git_lock = threading.Lock()
def git_cleanup():
    lock_file = ".git/index.lock"
    if os.path.exists(lock_file):
        try: os.remove(lock_file)
        except Exception: pass
def git_run(cmd, **kwargs):
    with _git_lock:
        git_cleanup()
        return subprocess.run(cmd, **kwargs)

def git_push_with_retry() -> bool:
    for attempt in range(3):
        try:
            git_run(["git","push"], check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError as e:
            log(f"Git push attempt {attempt+1} failed: {e.stderr.strip() if e.stderr else 'unknown'}")
            if attempt < 2:
                time.sleep(2 + random.random()*3)
                try: git_run(["git","pull","--rebase"], check=True, capture_output=True)
                except Exception: pass
    return False

# ---------- Profile cache (chunked) ----------
PROFILE_DIR = "/tmp/chrome_profile"
CACHE_DIR = ".profile_cache"
CHUNK_SIZE = 45 * 1024 * 1024
ENCRYPTION_KEY = None
try:
    KEY = os.environ["KEY"]
    ENCRYPTION_KEY = base64.urlsafe_b64encode(hashlib.sha256(KEY.encode()).digest())
except Exception as e:
    log(f"PROFILE KEY ERROR: {e}")
    raise

os.makedirs(CACHE_DIR, exist_ok=True)

def load_profile():
    chunks = sorted(glob.glob(os.path.join(CACHE_DIR, "profile.enc.part*")))
    if not chunks:
        log("No profile cache chunks found.")
        return False
    try:
        buf = io.BytesIO()
        for path in chunks:
            with open(path, "rb") as f:
                buf.write(f.read())
        encrypted = buf.getvalue()
        decrypted = Fernet(ENCRYPTION_KEY).decrypt(encrypted)
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        buf2 = io.BytesIO(decrypted)
        with tarfile.open(fileobj=buf2, mode='r:gz') as tar:
            tar.extractall('/tmp')
        log("Profile cache loaded (chunked).")
        return True
    except InvalidToken:
        log("ERROR: Profile cache corrupted – starting fresh.")
        for p in chunks: os.remove(p)
        return False
    except Exception as e:
        log(f"ERROR loading profile cache: {e}")
        return False

def save_profile():
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(PROFILE_DIR, arcname="chrome_profile")
        encrypted = Fernet(ENCRYPTION_KEY).encrypt(buf.getvalue())

        for old in glob.glob(os.path.join(CACHE_DIR, "profile.enc.part*")):
            os.remove(old)

        for i in range(0, len(encrypted), CHUNK_SIZE):
            chunk_data = encrypted[i:i+CHUNK_SIZE]
            part_name = f"profile.enc.part{i//CHUNK_SIZE:04d}"
            with open(os.path.join(CACHE_DIR, part_name), "wb") as f:
                f.write(chunk_data)
        log(f"Profile cache saved in {len(encrypted)//CHUNK_SIZE+1} chunks.")

        git_run(["git", "add", CACHE_DIR], check=True, capture_output=True)
        try:
            git_run(["git", "diff", "--cached", "--quiet"], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            git_run(["git", "commit", "-m", "Update profile cache chunks"], check=True, capture_output=True)
            git_push_with_retry()
    except Exception as e:
        log(f"ERROR saving profile cache: {e}")

if load_profile():
    log("✅ Profile cache loaded.")
else:
    log("⚠️ Starting without saved login data.")

# ---------- Periodic saver ----------
_profile_save_stop = threading.Event()
def periodic_save_worker():
    while not _profile_save_stop.is_set():
        _profile_save_stop.wait(300)
        if not _profile_save_stop.is_set():
            log("Periodic profile save triggered.")
            save_profile()
threading.Thread(target=periodic_save_worker, daemon=True).start()

# ---------- Browser setup ----------
DOWNLOAD_DIR = "/home/runner/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
try:
    display = Display(visible=False, size=(1920,1080))
    display.start()
    log("Virtual display started.")
    opts = Options()
    opts.add_argument("--no-sandbox"); opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    chrome_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    opts.add_argument(f"--user-agent={chrome_ua}")
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--profile-directory=Default")
    opts.add_experimental_option("prefs", {"download.default_directory": DOWNLOAD_DIR})
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option('useAutomationExtension', False)

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    driver.execute_script("Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]})")
    driver.execute_script("Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']})")
    driver.execute_script("window.chrome = { runtime: {} };")
    driver.execute_script("Object.defineProperty(navigator, 'permissions', {get: () => ({ query: () => Promise.resolve({ state: 'granted' }) })})")
    driver.execute_script("Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 4})")
    log("Stealth JS injected.")

    try:
        from upload_injector import _init_cdp
        if _init_cdp(driver, log):
            log("CDP interception active.")
    except Exception as e_cdp:
        log(f"CDP not available ({e_cdp}) – using send_keys fallback.")

    log("Browser launched.")
except Exception as e:
    log(f"BROWSER ERROR: {e}\n{traceback.format_exc()}")
    raise

# Sync globals to agent_state
agent_state = sys.modules.get("agent_state")
if agent_state:
    agent_state.driver = driver
    agent_state.W = 1920; agent_state.H = 1080
    agent_state.cursor_x = 960; agent_state.cursor_y = 540
    agent_state.DOWNLOAD_DIR = DOWNLOAD_DIR
    agent_state.allowed_secrets = allowed_secrets
    agent_state.HAS_GEMINI = HAS_GEMINI
    agent_state.HAS_PYPERCLIP = HAS_PYPERCLIP
    agent_state.pyperclip = pyperclip
    agent_state.log = log

# ---------- Screenshot ----------
counter = [0]

def ss(desc="screenshot", push=True, response_suffix=""):
    counter[0] += 1
    now = datetime.now().strftime("%H%M%S")
    sfx = re.sub(r'[^a-zA-Z0-9 _\-.(),]', '', response_suffix)[:60] if response_suffix else ""
    fname = f"screenshots/{counter[0]:03d}_{now}_{desc}_{sfx}.png" if sfx else f"screenshots/{counter[0]:03d}_{now}_{desc}.png"
    log(f"Taking screenshot: {fname}")
    driver.save_screenshot(fname)
    try:
        img = Image.open(fname); draw = ImageDraw.Draw(img)
        x, y = agent_state.cursor_x if agent_state else 960, agent_state.cursor_y if agent_state else 540
        r = 12
        draw.ellipse([(x-r,y-r),(x+r,y+r)], outline='red', width=3)
        draw.line([(x-15,y),(x+15,y)], fill='red', width=3)
        draw.line([(x,y-15),(x,y+15)], fill='red', width=3)
        img.save(fname)
    except Exception: pass
    if not push: return fname
    try:
        git_run(["git","stash","--include-untracked"], capture_output=True)
        try: git_run(["git","pull","--rebase"], check=True, capture_output=True)
        except Exception: pass
        git_run(["git","stash","pop"], capture_output=True)
        # Add only the screenshot and log – NOT the cache files
        git_run(["git","add",fname,LOG_FILENAME], check=True, capture_output=True)
        try: git_run(["git","diff","--cached","--quiet"], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            git_run(["git","commit","-m",f"Screenshot {fname}"], check=True, capture_output=True)
            if git_push_with_retry():
                log(f"Pushed {fname}")
            else:
                log(f"ERROR: Failed to push {fname}")
        # Purge old screenshots (may fail; ignore)
        try:
            purge_old_screenshots(os.path.basename(fname))
        except Exception: pass
    except Exception as e:
        log(f"Screenshot git error: {e}")
    return fname

def purge_old_screenshots(keep_filename):
    try:
        raw = gh(f"repos/{REPO}/contents/screenshots", "--jq", ".[].path")
        if not raw: return
        for path in raw.strip().splitlines():
            path = path.strip().strip('"')
            if not path.endswith(".png"): continue
            if path == "screenshots/" + keep_filename: continue
            sha_raw = gh(f"repos/{REPO}/contents/{path}", "--jq", ".sha")
            if not sha_raw: continue
            sha = sha_raw.strip().strip('"')
            gh("--method","DELETE",f"repos/{REPO}/contents/{path}",
               "-f","message=purge","-f",f"sha={sha}","-f","branch=main")
            log(f"Purged old: {path}")
    except Exception as e:
        log(f"Purge warning: {e}")

# ---------- Screenshot worker with auto‑restart ----------
_screenshot_stop = threading.Event()
_screenshot_thread = None
_screenshot_worker_running = False

def screenshot_worker():
    global _screenshot_worker_running
    _screenshot_worker_running = True
    while not _screenshot_stop.is_set():
        try:
            time.sleep(2)
            while not _screenshot_stop.is_set():
                start = time.time()
                try:
                    ss("auto", push=True)
                except Exception as e:
                    log(f"Screenshot worker error: {e}")
                    time.sleep(5)
                    continue
                elapsed = time.time() - start
                _screenshot_stop.wait(max(0, COMM_INTERVAL * slow_mode - elapsed))
        except Exception as outer_e:
            log(f"Screenshot worker crashed: {outer_e}. Restarting in 5s...")
            _screenshot_stop.wait(5)
    _screenshot_worker_running = False

def start_screenshot_worker():
    global _screenshot_thread
    if _screenshot_thread and _screenshot_thread.is_alive():
        return
    _screenshot_stop.clear()
    _screenshot_thread = threading.Thread(target=screenshot_worker, daemon=True)
    _screenshot_thread.start()
    log("Screenshot worker started.")

def monitor_screenshot_worker():
    """Watchdog that restarts the screenshot worker if it dies unexpectedly."""
    while not _screenshot_stop.is_set():
        time.sleep(10)
        if not _screenshot_thread or not _screenshot_thread.is_alive():
            log("Screenshot worker is dead! Restarting...")
            start_screenshot_worker()

# Start screenshot worker and its watchdog
start_screenshot_worker()
threading.Thread(target=monitor_screenshot_worker, daemon=True).start()

# ---------- GitHub basics ----------
ISSUE_NUMBER = os.environ.get("ISSUE_NUMBER","4").strip()
START_URL = os.environ.get("START_URL") or "https://studio.youtube.com"
REPO = os.environ['GITHUB_REPOSITORY']
def push_logs():
    try:
        git_run(["git","add",LOG_FILENAME], check=True, capture_output=True)
        try: git_run(["git","diff","--cached","--quiet"], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            git_run(["git","commit","-m","Log update"], check=True, capture_output=True)
            git_push_with_retry()
    except Exception as e: echo(f"Could not push logs: {e}")

KEY_SECRET = os.environ["KEY"]

def smart_edit_comment(comment_id, new_body):
    for attempt in range(2):
        try:
            edit_comment(REPO, comment_id, new_body)
            return True
        except subprocess.CalledProcessError:
            if attempt == 0:
                log("Edit rate‑limited, retrying in 2s...")
                time.sleep(2)
            else:
                log("Edit failed after retry.")
    return False

# ---------- Main loop ----------
def main():
    global slow_mode, last_command_time

    try:
        log("Loading start URL...")
        driver.get(START_URL)
        log("Start URL loaded – sleeping 5s")
        time.sleep(5)
        log("Scrolling to top")
        driver.execute_script("window.scrollTo(0,0);")
        log("Taking first screenshot")
        ss("01_start_page", push=True)
        log("First screenshot saved")
        refresh_known_handles()
        agent_state._last_known_url = driver.current_url
        threading.Thread(target=url_monitor_worker, daemon=True).start()
        log("URL monitor started")
    except Exception as e:
        log(f"FATAL STARTUP: {e}\n{traceback.format_exc()}")
        push_logs()
        raise

    RESPONSE_MARKER = "## Remote Agent Responses"
    APP_COMMAND_MARKER = "## App Commands"
    try:
        all_comments = get_all_comments(REPO, ISSUE_NUMBER)
        log(f"Fetched {len(all_comments)} comments.")
    except Exception as e:
        log(f"ERROR fetching comments: {e}\n{traceback.format_exc()}")
        push_logs()
        raise

    resp_comment = find_marker_comment(all_comments, RESPONSE_MARKER)
    if resp_comment: response_comment_id = resp_comment["id"]; log(f"Found response comment {response_comment_id}")
    else: response_comment_id = issue_comment(REPO, ISSUE_NUMBER, RESPONSE_MARKER+"\n"); log(f"Created response comment {response_comment_id}")

    app_cmd = find_marker_comment(all_comments, APP_COMMAND_MARKER)
    app_cmd_id = app_cmd["id"] if app_cmd else None
    log(f"App command comment: {app_cmd_id}")

    keep = {response_comment_id}
    if app_cmd_id: keep.add(app_cmd_id)
    for c in all_comments:
        if c["id"] not in keep: delete_comment(REPO, c["id"])
    log("Old comments cleaned."); push_logs()

    if app_cmd_id:
        try: edit_comment(REPO, app_cmd_id, "## App Commands\n"); log("App command comment blanked.")
        except Exception as e: log(f"Could not blank: {e}")

    executed_cache = {}; unsent_reports = []; exec_queue = ExecutionQueue()

    def publish_reports(comment_id):
        cull_expired_autonomous_reports()
        lines = ["## Remote Agent Responses"]
        for ts, seq, result in unsent_reports:
            lines.append(f"[{ts}]: response to command number [{seq}]: {result}")
        for r in pending_autonomous_reports:
            lines.append(f"{r['id']}; {r['text']}")
        body = "\n".join(lines)
        pending_autonomous_reports.clear()
        if not comment_exists(REPO, comment_id):
            for _ in range(3):
                try:
                    new_id = issue_comment(REPO, ISSUE_NUMBER, body)
                    log(f"Created new response comment: {new_id}")
                    unsent_reports.clear(); push_logs()
                    return new_id
                except Exception as e: log(f"Failed to create response: {e}"); time.sleep(2)
            return comment_id
        if smart_edit_comment(comment_id, body):
            unsent_reports.clear(); push_logs(); return comment_id
        return comment_id

    seq_pattern = re.compile(r'^APP-(\d+)-')

    while True:
        if time.time() - last_command_time > 120:
            slow_mode = 15
        else:
            slow_mode = 1

        try:
            if app_cmd_id:
                try: _ = gh(f"repos/{REPO}/issues/comments/{app_cmd_id}", "--jq", ".id")
                except subprocess.CalledProcessError:
                    log(f"App command comment {app_cmd_id} vanished – resetting.")
                    app_cmd_id = None

            if not app_cmd_id:
                time.sleep(COMM_INTERVAL * slow_mode)
                allc = get_all_comments(REPO, ISSUE_NUMBER)
                app_c = find_marker_comment(allc, APP_COMMAND_MARKER)
                if app_c:
                    app_cmd_id = app_c["id"]; log(f"Re‑found app cmd: {app_cmd_id}")
                else:
                    try:
                        app_cmd_id = issue_comment(REPO, ISSUE_NUMBER, "## App Commands\n")
                        log(f"Created new app cmd: {app_cmd_id}")
                    except Exception as ce: log(f"Could not create app cmd: {ce}")
                continue

            app_body = gh(f"repos/{REPO}/issues/comments/{app_cmd_id}", "--jq", ".body")
            if not app_body: time.sleep(COMM_INTERVAL * slow_mode); continue
            lines = app_body.strip().splitlines()
            if not lines: time.sleep(COMM_INTERVAL * slow_mode); continue

            capture = False
            for line in lines:
                m = re.match(r'^\[(\d+)\]: app commands:', line)
                if m: capture = True; continue
                if capture:
                    if re.match(r'^\[', line): break
                    parts = line.split(';', 1)
                    if len(parts) == 2:
                        cid = parts[0].strip(); ctext = parts[1].strip()
                        if ctext:
                            seq_match = seq_pattern.match(cid)
                            seq = int(seq_match.group(1)) if seq_match else int(time.time())
                            if cid in executed_cache:
                                ts, _, cached_result = executed_cache[cid]
                                unsent_reports.append((ts, seq, cached_result))
                                log(f"Cached: {cid}")
                            else:
                                exec_queue.add_command(cid, ctext)
                                log(f"Queued: {cid} → {ctext}")
                                last_command_time = time.time()

            should_exit = False
            while True:
                item = exec_queue.pop_next()
                if item is None: break
                cid, ctext = item["id"], item["text"]
                cmd_type, arg = parse_single_command(ctext)

                result = execute_one_command(
                    cmd_type, arg,
                    driver=driver, cursor_x=agent_state.cursor_x, cursor_y=agent_state.cursor_y,
                    W=agent_state.W, H=agent_state.H, DOWNLOAD_DIR=DOWNLOAD_DIR, LOG_FILENAME=LOG_FILENAME,
                    KEY_SECRET=KEY_SECRET, REPO=REPO, ISSUE_NUMBER=ISSUE_NUMBER,
                    HAS_GEMINI=HAS_GEMINI, HAS_PYPERCLIP=HAS_PYPERCLIP,
                    allowed_secrets=allowed_secrets, ENCRYPTION_KEY=ENCRYPTION_KEY,
                    human_click_callable=human_click, human_click_at_callable=human_click_at,
                    _try_gemini_click=_try_gemini_click,
                    move_cursor_absolute=move_cursor_absolute,
                    move_cursor_relative=move_cursor_relative,
                    left_click=left_click, left_button_down=left_button_down,
                    left_button_up=left_button_up, right_button_down=right_button_down,
                    right_button_up=right_button_up, middle_button_down=middle_button_down,
                    middle_button_up=middle_button_up, double_click=double_click,
                    right_click=right_click, middle_click=middle_click,
                    scroll_by=scroll_by, drag_from_to=drag_from_to,
                    press_key=press_key, press_combo=press_combo,
                    type_secret=type_secret, decode_string=decode_string,
                    ss=ss, refresh_file_registry=refresh_file_registry,
                    add_autonomous_report=add_autonomous_report,
                    refresh_known_handles=refresh_known_handles,
                    get_upload_paths=get_upload_paths, save_profile=save_profile,
                    _file_registry=_file_registry, _upload_file_paths=_upload_file_paths,
                    pyperclip=pyperclip if HAS_PYPERCLIP else None,
                    upload_reassemble=upload_reassemble,
                    HAS_PYPERCLIP_local=HAS_PYPERCLIP,
                    encrypt_string=encrypt_string, gh=gh,
                    get_all_comments=get_all_comments,
                    delete_comment=delete_comment, issue_comment=issue_comment,
                    smart_edit_comment=smart_edit_comment, git_push_with_retry=git_push_with_retry,
                    comm_interval=COMM_INTERVAL * slow_mode, inject_file=None
                )

                ts = int(time.time())
                seq_match = seq_pattern.match(cid)
                seq = int(seq_match.group(1)) if seq_match else 0
                executed_cache[cid] = (ts, seq, result)
                unsent_reports.append((ts, seq, result))
                log(f"Executed: {cid} → {result}")
                if cmd_type == "exit":
                    should_exit = True
                    break

            response_comment_id = publish_reports(response_comment_id)
            if should_exit:
                log("Exit command received – saving profile cache...")
                save_profile()
                time.sleep(1)
                response_comment_id = publish_reports(response_comment_id)
                ss("final", push=True)
                _profile_save_stop.set()
                _screenshot_stop.set()
                _url_monitor_stop.set()
                driver.quit(); display.stop()
                push_logs()
                echo("\n🎉 Remote session ended.")
                sys.exit(0)
        except Exception as e:
            log(f"Polling error: {e}\n{traceback.format_exc()}")
            push_logs()
            time.sleep(2 * slow_mode)

if __name__ == "__main__":
    try:
        main()
    except SystemExit: pass
    except Exception as ex:
        log(f"FATAL: {ex}\n{traceback.format_exc()}")
        push_logs()
    finally:
        _profile_save_stop.set()
        _screenshot_stop.set()
        _url_monitor_stop.set()
        save_profile()
        push_logs()
        _log_closed = True
        try: _logfile.close()
        except Exception: pass
