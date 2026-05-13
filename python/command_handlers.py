#!/usr/bin/env python3
# ==============================================================================
# command_handlers.py – Version 1.11.0
#   - Scroll targets the element under the cursor first
#   - Navigate does not block on downloadable files (uses location.href)
# ==============================================================================
import os, time, subprocess, glob, shutil, re, tempfile, random
from uploader import reassemble
from upload_handler import perform_upload
from upload_injector import upload_to_youtube

def _ensure_selection(_file_registry, _upload_file_paths):
    if not _upload_file_paths and _file_registry:
        first_id = min(_file_registry.keys())
        _upload_file_paths.append(_file_registry[first_id])

def _scroll_element_or_window(driver, amount, cursor_x, cursor_y):
    """
    Scroll the element under the cursor if it has a scrollable overflow,
    otherwise scroll the active element, and finally fall back to the window.
    """
    direction = "down" if amount >= 0 else "up"
    # 1. Try the element under the cursor
    try:
        elem = driver.execute_script(
            "return document.elementFromPoint(arguments[0], arguments[1]);",
            cursor_x, cursor_y)
        if elem:
            sh = driver.execute_script("return arguments[0].scrollHeight;", elem)
            ch = driver.execute_script("return arguments[0].clientHeight;", elem)
            if sh > ch:
                driver.execute_script("arguments[0].scrollBy(0, arguments[1]);", elem, amount)
                return f"OK scroll({direction},{abs(amount)}) [under cursor]"
    except Exception:
        pass
    # 2. Try the active element
    try:
        elem = driver.switch_to.active_element
        if elem:
            sh = driver.execute_script("return arguments[0].scrollHeight;", elem)
            ch = driver.execute_script("return arguments[0].clientHeight;", elem)
            if sh > ch:
                driver.execute_script("arguments[0].scrollBy(0, arguments[1]);", elem, amount)
                return f"OK scroll({direction},{abs(amount)}) [active element]"
    except Exception:
        pass
    # 3. Fall back to window
    driver.execute_script(f"window.scrollBy(0, {amount});")
    return f"OK scroll({direction},{abs(amount)}) [window]"

def execute_one_command(
    cmd, arg,
    driver, cursor_x, cursor_y, W, H,
    DOWNLOAD_DIR, LOG_FILENAME,
    KEY_SECRET, REPO, ISSUE_NUMBER,
    HAS_GEMINI, HAS_PYPERCLIP, allowed_secrets, ENCRYPTION_KEY,
    human_click_callable, human_click_at_callable, _try_gemini_click,
    move_cursor_absolute, move_cursor_relative,
    left_click, left_button_down, left_button_up,
    right_button_down, right_button_up,
    middle_button_down, middle_button_up,
    double_click, right_click, middle_click,
    scroll_by, drag_from_to,
    press_key, press_combo, type_secret,
    decode_string, ss,
    refresh_file_registry, add_autonomous_report, refresh_known_handles,
    get_upload_paths, save_profile,
    _file_registry, _upload_file_paths,
    pyperclip, upload_reassemble,
    HAS_PYPERCLIP_local, encrypt_string, gh,
    get_all_comments, delete_comment, issue_comment,
    smart_edit_comment, git_push_with_retry,
    comm_interval=5.0,
    inject_file=None
):
    result = ""

    if cmd == "exit": 
        result = "OK exit"
    elif cmd == "screenshot":
        if callable(ss):
            result = f"OK screenshot at ({cursor_x},{cursor_y})"
            ss("manual_screenshot", push=True, response_suffix=result)
        else: result = "ERR screenshot not available"
    elif cmd == "move":
        x, y = arg; move_cursor_absolute(x, y)
        result = f"OK move({cursor_x},{cursor_y})"
    elif cmd == "moveby":
        dx, dy = arg; move_cursor_relative(dx, dy)
        result = f"OK moveby({dx},{dy})->({cursor_x},{cursor_y})"
    elif cmd == "click_at":
        x, y = arg; move_cursor_absolute(x, y); left_click()
        result = f"OK click({cursor_x},{cursor_y})"
    elif cmd == "humanclick": result = human_click_callable()
    elif cmd == "humanclick_at": x, y = arg; result = human_click_at_callable(x, y)
    elif cmd == "leftdown":   left_button_down(); result = "OK leftdown"
    elif cmd == "leftup":     left_button_up();   result = "OK leftup"
    elif cmd == "rightdown":  right_button_down(); result = "OK rightdown"
    elif cmd == "rightup":    right_button_up();  result = "OK rightup"
    elif cmd == "middledown": middle_button_down(); result = "OK middledown"
    elif cmd == "middleup":   middle_button_up();  result = "OK middleup"
    elif cmd == "refresh":     driver.refresh(); time.sleep(3); result = "OK refresh"
    elif cmd == "shoot":       left_click(); result = f"OK click({cursor_x},{cursor_y})"
    elif cmd == "doubleshoot": double_click(); result = "OK doubleclick"
    elif cmd == "rightshoot":  right_click();  result = "OK rightclick"
    elif cmd == "middleshoot": middle_click(); result = "OK middleclick"
    elif cmd == "scroll":
        result = _scroll_element_or_window(driver, int(arg), cursor_x, cursor_y)
    elif cmd == "wait":        time.sleep(arg / 1000.0); result = f"OK wait({arg}ms)"
    elif cmd == "key":         press_key(arg); result = f"OK key({arg})"
    elif cmd == "combo":       press_combo(arg); result = f"OK combo({arg})"
    elif cmd == "secret":
        ok = type_secret(arg)
        result = f"OK secret({arg})" if ok else f"ERR secret({arg})"
    elif cmd == "decode":
        plain = decode_string(arg, KEY_SECRET)
        if plain is None: result = "ERR decode"
        else:
            try:
                elem = driver.switch_to.active_element
                elem.send_keys(plain)
                result = "OK decode"
            except Exception as e:
                result = f"ERR decode: {e}"
    elif cmd == "humantype":
        plain = decode_string(arg, KEY_SECRET)
        if plain is None: result = "ERR humantype"
        else:
            try:
                elem = driver.switch_to.active_element
                for ch in plain:
                    elem.send_keys(ch)
                    time.sleep(random.uniform(0.03, 0.12))
                result = "OK humantype"
            except Exception as e:
                result = f"ERR humantype: {e}"
    elif cmd == "navigate":
        # Use location.href to avoid blocking on downloads
        driver.execute_script("window.location.href = arguments[0];", arg)
        time.sleep(1.5)   # give the browser a moment to start the request
        current_url = driver.current_url
        result = f"OK navigate({current_url})"
        add_autonomous_report("navigate", f"navigate({current_url})")
        refresh_known_handles()
    elif cmd == "drag":
        x1, y1, x2, y2 = arg
        drag_from_to(x1, y1, x2, y2)
        result = f"OK drag({x1},{y1})->({x2},{y2})"
    elif cmd == "download":
        downloaded = glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
        if not downloaded: result = "ERR download: no files in download folder"
        else:
            count = 0
            for fpath in downloaded:
                if os.path.isfile(fpath):
                    fname = os.path.basename(fpath)
                    out_dir = os.path.join("downloaded_chunks", fname)
                    os.makedirs(out_dir, exist_ok=True)
                    subprocess.run(["python3", "chunker.py", "--file", fpath, "--output-dir", out_dir, "--chunk-size", "20"], check=True)
                    count += 1
            subprocess.run(["git", "add", "downloaded_chunks/", LOG_FILENAME], check=True)
            try: subprocess.run(["git", "diff", "--cached", "--quiet"], check=True)
            except subprocess.CalledProcessError:
                subprocess.run(["git", "commit", "-m", "Downloaded files chunked"], check=True)
                git_push_with_retry()
            result = f"OK download({count} files chunked)"
        refresh_file_registry()
        _ensure_selection(_file_registry, _upload_file_paths)
    elif cmd == "upload":
        log_func = None
        try: log_func = __import__("sys").modules["__main__"].log
        except Exception: pass
        result = perform_upload(
            DOWNLOAD_DIR, LOG_FILENAME,
            refresh_file_registry, add_autonomous_report,
            _file_registry, _upload_file_paths,
            git_push_with_retry, inject_file, log_func=log_func
        )
        _ensure_selection(_file_registry, _upload_file_paths)
    elif cmd == "injectfile":
        if not callable(inject_file): result = "ERR injectfile not available"
        else:
            if inject_file(): result = "OK injectfile (file injected)"
            else: result = "ERR injectfile failed – no file selected or no dialog open"
    elif cmd == "uploadtoyoutube":
        refresh_file_registry()
        _ensure_selection(_file_registry, _upload_file_paths)
        paths = get_upload_paths()
        if not paths: result = "ERR uploadtoyoutube: no file selected"
        else:
            log_func = None
            try: log_func = __import__("sys").modules["__main__"].log
            except Exception: pass
            if upload_to_youtube(driver, paths[0], log_func): result = "OK uploadtoyoutube (injected)"
            else: result = "ERR uploadtoyoutube: injection failed"
    elif cmd == "paste":
        if not HAS_PYPERCLIP_local: result = "ERR paste: pyperclip not installed"
        else:
            try:
                clip_text = pyperclip.paste()
                if not clip_text: result = "OK paste (empty clipboard)"
                else:
                    encoded = encrypt_string(clip_text, KEY_SECRET)
                    paste_body = "## Paste Data\n" + encoded
                    allc = get_all_comments(REPO, ISSUE_NUMBER)
                    for c in allc:
                        if c.get("body", "").startswith("## Paste Data"): delete_comment(REPO, c["id"])
                    issue_comment(REPO, ISSUE_NUMBER, paste_body)
                    result = f"OK paste ({len(clip_text)} chars)"
            except Exception as e: result = f"ERR paste: {e}"
    elif cmd == "tabs":
        for _ in range(20):
            if driver.window_handles: break
            time.sleep(0.5)
        current_handle = driver.current_window_handle
        handles = driver.window_handles
        lines = []
        for i, h in enumerate(handles):
            try:
                driver.switch_to.window(h)
                title = (driver.title or "Untitled")[:60]
            except: title = "(error)"
            lines.append(f"{i+1}: {title}")
        try: driver.switch_to.window(current_handle)
        except:
            if handles: driver.switch_to.window(handles[0])
        result = "Tabs: " + " | ".join(lines)
        refresh_known_handles()
    elif cmd == "dir":
        refresh_file_registry()
        _ensure_selection(_file_registry, _upload_file_paths)
        if not _file_registry: result = "Files: (empty)"
        else:
            lines = [f"{fid}: {fname}" for fid, fname in sorted(_file_registry.items())]
            result = "Files: " + " | ".join(lines)
    elif cmd == "tabnumber":
        try:
            idx = int(arg) - 1
            handles = driver.window_handles
            if not handles: result = "ERR tabnumber: no window handles"
            elif 0 <= idx < len(handles):
                driver.switch_to.window(handles[idx])
                try: driver.set_window_size(W, H)
                except Exception: pass
                result = f"Switched to tab {idx+1}: {driver.title[:40]}"
            else:
                time.sleep(0.5); handles = driver.window_handles
                if 0 <= idx < len(handles):
                    driver.switch_to.window(handles[idx])
                    try: driver.set_window_size(W, H)
                    except Exception: pass
                    result = f"Switched to tab {idx+1}: {driver.title[:40]}"
                else: result = "ERR: invalid tab number"
        except Exception as e: result = f"ERR tabnumber: {e}"
    elif cmd == "closetab":
        try:
            idx = int(arg) - 1; handles = driver.window_handles
            if 0 <= idx < len(handles) and len(handles) > 1:
                driver.switch_to.window(handles[idx]); driver.close()
                handles = driver.window_handles; driver.switch_to.window(handles[0])
                result = f"Closed tab {idx+1}"
            else: result = "ERR: cannot close last tab"
        except: result = "ERR: invalid tab number"
    elif cmd == "lastdownload": result = "Last download: (not implemented)"
    elif cmd == "uploadnumber":
        try:
            ids = [int(x.strip()) for x in arg.split(",") if x.strip().isdigit()]
            refresh_file_registry()
            new_paths = [_file_registry[fid] for fid in ids if fid in _file_registry]
            if new_paths:
                _upload_file_paths.clear()
                _upload_file_paths.extend(new_paths)
                result = f"Upload file(s) set to: {list(_upload_file_paths)}"
            else:
                _ensure_selection(_file_registry, _upload_file_paths)
                result = f"Upload file(s) unchanged (no valid IDs in {arg})"
        except Exception: result = "ERR invalid upload numbers"
    elif cmd == "savestate":
        save_profile(); result = "OK savestate"
    elif cmd == "save":
        save_profile(); result = "OK save"
    elif cmd == "setinterval":
        import sys
        main_mod = sys.modules.get("__main__")
        if main_mod: main_mod.COMM_INTERVAL = float(arg); result = f"OK interval set to {main_mod.COMM_INTERVAL}s"
        else: result = "ERR setinterval"
    else: result = f"ERR unknown cmd:{cmd}"

    refresh_known_handles()
    return result
