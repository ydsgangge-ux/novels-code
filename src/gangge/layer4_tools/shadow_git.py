"""Shadow Git — automatic checkpoint & rollback for AI file modifications.

Before each AI task execution, creates a git checkpoint.
If the AI produces broken code, user can one-click rollback.

Usage:
    from gangge.layer4_tools.shadow_git import ShadowGit

    sg = ShadowGit(workspace="/path/to/project")
    sg.checkpoint("before: implement login feature")
    # ... AI modifies files ...
    sg.checkpoint("after: login feature done")
    # List checkpoints
    checkpoints = sg.list_checkpoints()
    # Rollback to a checkpoint
    sg.rollback("before: implement login feature")
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHECKPOINT_PREFIX = "gangge-checkpoint"


class ShadowGit:
    """Manages automatic git checkpoints for AI-driven file modifications."""

    def __init__(self, workspace: str):
        self.workspace = workspace
        self._git_dir = Path(workspace) / ".git"
        self._initialized = False

    def is_available(self) -> bool:
        return self._git_dir.exists()

    def ensure_init(self, user_name: str = "", user_email: str = "") -> bool:
        if self.is_available():
            self._initialized = True
            return True
        try:
            self._run(["git", "init"], check=True)
            name = user_name or "Gangge Code"
            email = user_email or "gangge@ai"
            self._run(["git", "config", "user.email", email], check=True)
            self._run(["git", "config", "user.name", name], check=True)
            self._gitignore_essentials()
            self._run(["git", "add", "-A"], check=True)
            self._run(["git", "commit", "-m", "gangge: initial checkpoint", "--allow-empty"], check=True)
            self._initialized = True
            return True
        except Exception as e:
            logger.warning(f"ShadowGit init failed: {e}")
            return False

    def _gitignore_essentials(self):
        gi_path = Path(self.workspace) / ".gitignore"
        lines = set()
        if gi_path.exists():
            lines = set(gi_path.read_text(encoding="utf-8", errors="replace").splitlines())
        essentials = {
            "__pycache__/", "*.pyc", ".env", "node_modules/",
            ".venv/", "venv/", "dist/", "build/", ".idea/", ".vscode/",
        }
        additions = essentials - lines
        if additions:
            with open(gi_path, "a", encoding="utf-8") as f:
                if lines and not any(l.strip() == "" for l in lines):
                    f.write("\n")
                for line in sorted(additions):
                    f.write(line + "\n")

    def checkpoint(self, label: str = "") -> str | None:
        if not self.is_available() and not self.ensure_init():
            return None
        try:
            self._run(["git", "add", "-A"])
            diff_stat = self._run(["git", "diff", "--cached", "--stat"])
            stat_line = (diff_stat.stdout or "").strip().splitlines()[-1] if diff_stat.stdout else ""
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            short_label = label[:80].replace("\n", " ") if label else "auto checkpoint"
            msg = f"{short_label}\n\nTimestamp: {ts}"
            if stat_line:
                msg += f"\n{stat_line}"
            result = self._run(["git", "commit", "-m", msg])
            if result.returncode == 0:
                hash_result = self._run(["git", "rev-parse", "--short", "HEAD"])
                short_hash = (hash_result.stdout or "").strip()
                logger.info(f"Checkpoint created: {short_hash} {short_label}")
                return short_hash
            return None
        except Exception as e:
            logger.warning(f"Checkpoint failed: {e}")
            return False

    def list_checkpoints(self, limit: int = 20) -> list[dict[str, str]]:
        if not self.is_available():
            return []
        try:
            result = self._run(
                ["git", "log", f"--max-count={limit}", "--pretty=format:%h|%ai|%s"],
            )
            checkpoints = []
            for line in (result.stdout or "").strip().splitlines():
                if "|" not in line:
                    continue
                parts = line.split("|", 2)
                if len(parts) < 3:
                    continue
                short_hash, date, msg = parts
                is_ours = msg.startswith(CHECKPOINT_PREFIX) or msg.startswith("gangge:")
                checkpoints.append({
                    "hash": short_hash,
                    "date": date,
                    "message": msg,
                    "is_checkpoint": is_ours,
                })
            return checkpoints
        except Exception:
            return []

    def rollback(self, ref: str = "HEAD~1") -> bool:
        if not self.is_available():
            return False
        try:
            if ref in ("HEAD~1", "HEAD~2", "HEAD~3"):
                pass
            elif len(ref) >= 7:
                pass
            else:
                ref = f"HEAD~{ref}"
            self._run(["git", "reset", "--hard", ref], check=True)
            self._run(["git", "clean", "-fd"], check=False)
            logger.info(f"Rolled back to {ref}")
            return True
        except Exception as e:
            logger.error(f"Rollback failed: {e}")
            return False

    def diff_since(self, ref: str = "HEAD~1") -> str:
        if not self.is_available():
            return ""
        try:
            result = self._run(["git", "diff", ref])
            return result.stdout or ""
        except Exception:
            return ""

    def status(self) -> dict[str, Any]:
        if not self.is_available():
            return {"available": False}
        try:
            branch_result = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
            branch = (branch_result.stdout or "").strip()
            status_result = self._run(["git", "status", "--short"])
            changed = [l for l in (status_result.stdout or "").strip().splitlines() if l.strip()]
            return {
                "available": True,
                "branch": branch,
                "changed_files": len(changed),
                "changed_details": changed[:20],
            }
        except Exception:
            return {"available": False}

    def user_commit(self, message: str = "") -> dict[str, Any]:
        if not self.is_available() and not self.ensure_init():
            return {"success": False, "error": "Git not initialized"}
        try:
            self._run(["git", "add", "-A"], check=True)
            status_result = self._run(["git", "status", "--short"])
            changed = [l for l in (status_result.stdout or "").strip().splitlines() if l.strip()]
            if not changed:
                log_result = self._run(["git", "log", "-1", "--pretty=format:%h|%s"])
                last_line = (log_result.stdout or "").strip()
                if "|" in last_line:
                    last_hash, last_msg = last_line.split("|", 1)
                    return {
                        "success": True,
                        "message": "Already up to date",
                        "files": 0,
                        "last_hash": last_hash,
                        "last_message": last_msg,
                    }
                return {"success": True, "message": "Already up to date", "files": 0}
            if not message:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                message = f"save: {ts}"
            result = self._run(["git", "commit", "-m", message])
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                if "nothing to commit" in err:
                    return {"success": True, "message": "Already up to date", "files": 0}
                return {"success": False, "error": err}
            hash_result = self._run(["git", "rev-parse", "--short", "HEAD"])
            short_hash = (hash_result.stdout or "").strip()

            diff_stat = self._run(["git", "diff", "--stat", "HEAD~1", "HEAD"])
            stat_lines = (diff_stat.stdout or "").strip().splitlines()
            total_line = stat_lines[-1] if stat_lines else ""
            file_details = []
            for line in stat_lines[:-1]:
                parts = line.split("|")
                if len(parts) >= 2:
                    fname = parts[0].strip()
                    stat = parts[1].strip()
                    file_details.append({"file": fname, "stat": stat})

            name_status = self._run(["git", "diff", "--name-status", "HEAD~1", "HEAD"])
            ns_map = {}
            for line in (name_status.stdout or "").strip().splitlines():
                if line.strip():
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        ns_map[parts[1]] = parts[0]

            insertions = 0
            deletions = 0
            for m in re.finditer(r"(\d+) insertion", total_line):
                insertions += int(m.group(1))
            for m in re.finditer(r"(\d+) deletion", total_line):
                deletions += int(m.group(1))

            return {
                "success": True,
                "hash": short_hash,
                "message": message,
                "files": len(changed),
                "insertions": insertions,
                "deletions": deletions,
                "file_details": file_details,
                "name_status": ns_map,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def user_push(self, remote: str = "origin", branch: str = "") -> dict[str, Any]:
        if not self.is_available() and not self.ensure_init():
            return {"success": False, "error": "Git not initialized"}
        try:
            if not branch:
                branch_result = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
                branch = (branch_result.stdout or "").strip()
            remote_result = self._run(["git", "remote", "-v"])
            if not (remote_result.stdout or "").strip():
                return {"success": False, "error": "NO_REMOTE", "hint": "No remote configured. Please add a remote first."}
            has_upstream = self._has_upstream(branch)
            push_cmd = ["git", "push"]
            if not has_upstream:
                push_cmd += ["-u", remote, branch]
            else:
                push_cmd += [remote, branch]
            result = self._run(push_cmd, timeout=60)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                return {"success": False, "error": err}
            return {"success": True, "remote": remote, "branch": branch, "first_push": not has_upstream}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Push timed out (60s). Check network connection."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _has_upstream(self, branch: str = "") -> bool:
        try:
            if not branch:
                branch_result = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
                branch = (branch_result.stdout or "").strip()
            result = self._run(["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
            return result.returncode == 0 and bool((result.stdout or "").strip())
        except Exception:
            return False

    def add_remote(self, name: str, url: str) -> dict[str, Any]:
        if not self.is_available() and not self.ensure_init():
            return {"success": False, "error": "Git not initialized"}
        try:
            existing = self._run(["git", "remote", "-v"])
            if name in (existing.stdout or ""):
                self._run(["git", "remote", "set-url", name, url], check=True)
            else:
                self._run(["git", "remote", "add", name, url], check=True)
            return {"success": True, "name": name, "url": url}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def create_github_repo(token: str, repo_name: str, private: bool = False, description: str = "") -> dict[str, Any]:
        try:
            import urllib.request
            import urllib.error
            data = json.dumps({
                "name": repo_name,
                "private": private,
                "description": description,
                "auto_init": False,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.github.com/user/repos",
                data=data,
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            clone_url = result.get("clone_url", "")
            ssh_url = result.get("ssh_url", "")
            html_url = result.get("html_url", "")
            full_name = result.get("full_name", "")
            return {
                "success": True,
                "clone_url": clone_url,
                "ssh_url": ssh_url,
                "html_url": html_url,
                "full_name": full_name,
                "private": private,
            }
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            err_msg = f"HTTP {e.code}"
            if "already exists" in body:
                err_msg = "Repository already exists on your GitHub account"
            elif e.code == 401:
                err_msg = "Invalid GitHub token. Please check your token in Settings."
            elif e.code == 403:
                err_msg = "Token does not have 'repo' scope. Please create a token with repo permissions."
            return {"success": False, "error": err_msg, "detail": body}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def get_github_username(token: str) -> str:
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return result.get("login", "")
        except Exception:
            return ""

    def get_remote_url(self, name: str = "origin") -> str:
        if not self.is_available():
            return ""
        result = self._run(["git", "remote", "get-url", name])
        return (result.stdout or "").strip()

    def get_current_branch(self) -> str:
        if not self.is_available():
            return ""
        result = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        return (result.stdout or "").strip()

    def _run(self, cmd: list[str], check: bool = False, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=check,
        )
