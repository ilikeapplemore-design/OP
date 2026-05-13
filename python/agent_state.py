# ---------- FILE REGISTRY & UPLOAD PATHS ----------
_file_registry = {}
_previous_file_set = set()
_upload_file_paths = []
_last_reported_files_str = None   # track last reported state to avoid spam

DOWNLOAD_DIR = ""   # set by main

def refresh_file_registry():
    """
    Scan DOWNLOAD_DIR and update the file registry IN‑PLACE.
    Only add an autonomous report if the file list actually changed.
    """
    global _file_registry, _previous_file_set, _last_reported_files_str
    try:
        files = sorted([f for f in os.listdir(DOWNLOAD_DIR) if not f.endswith(".crdownload")])
        new_set = set(files)
        new_files = new_set - _previous_file_set
        for nf in new_files:
            add_autonomous_report("filedownloaded", f"New file: {nf}")
        _previous_file_set = new_set

        # update the existing dict in‑place
        _file_registry.clear()
        for i, fname in enumerate(files, start=1):
            _file_registry[i] = fname

        # Build a canonical string of current files
        if _file_registry:
            lines = [f"{fid}: {fname}" for fid, fname in sorted(_file_registry.items())]
            current_str = "Files: " + " | ".join(lines)
        else:
            current_str = "Files: (empty)"

        # Only report if it changed since last time
        if current_str != _last_reported_files_str:
            _last_reported_files_str = current_str
            add_autonomous_report("files", current_str)

    except Exception as e:
        try:
            log(f"ERROR refreshing file registry: {e}")
        except:
            pass
