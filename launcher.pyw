"""QQ AI 机器人 桌面一键启动器。

双击本文件运行（.pyw 无黑色控制台窗口）：
  - 一键启动：NapCat(快速登录) → 机器人 → 自动打开管理控制台
  - 也可单独启动/停止 NapCat 或机器人
  - 实时状态与日志显示

私密信息（QQ号、路径）从 config.toml 的 [launcher] 段读取，本文件不含任何私密信息。
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import messagebox, scrolledtext

# PyInstaller 打包后以 exe 所在目录为根；同时静态引用 bot 模块确保被打进 exe
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
if FROZEN:
    import bot as _bot_module  # noqa: F401  打包时把机器人主程序一并收进 exe

CONFIG_PATH = ROOT / "config.toml"
DATA_DIR = ROOT / "data"
NAPCAT_PORT = 3001
WEBUI_PORT = 6099
PLUGIN_ID = "napcat-plugin-qq-ai-bot"
CONSOLE_URL = f"http://127.0.0.1:{WEBUI_PORT}/plugin/{PLUGIN_ID}/page/qq-ai-bot"


def read_bot_port() -> int:
    """机器人管理 API 用系统随机端口，实际端口写在 data/api.port。"""
    try:
        p = int((DATA_DIR / "api.port").read_text(encoding="ascii").strip())
        return p if p > 0 else 0
    except (OSError, ValueError):
        return 0


_last_open: dict[str, float] = {}


def open_url(url: str) -> None:
    """打开浏览器页面。os.startfile 单次打开 + 1 秒防抖，
    避免浏览器冷启动时把启动参数处理两次导致双开标签。"""
    now = time.time()
    if now - _last_open.get(url, 0.0) < 1.0:
        return
    _last_open[url] = now
    os.startfile(url)


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    defaults = {"napcat_dir": r"E:\NapCat\shell", "qq_path": "", "qq_account": 0}
    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f).get("launcher", {})
    except (OSError, tomllib.TOMLDecodeError):
        cfg = {}
    defaults.update({k: cfg[k] for k in defaults if k in cfg})
    return defaults


def find_qq_exe(configured: str) -> str | None:
    if configured and Path(configured).exists():
        return configured
    try:
        out = subprocess.run(
            ["reg", "query", r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
             "/v", "UninstallString"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        m = re.search(r"UninstallString\s+REG_SZ\s+\"?(.+?Uninstall\.exe)", out)
        if m:
            qq = Path(m.group(1)).parent / "QQ.exe"
            if qq.exists():
                return str(qq)
    except (OSError, subprocess.SubprocessError):
        pass
    for p in (r"C:\Program Files\Tencent\QQNT\QQ.exe", r"D:\Program Files\Tencent\QQNT\QQ.exe"):
        if Path(p).exists():
            return p
    return None


def port_open(port: int) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def bot_alive() -> bool:
    """机器人进程存活 = 它写出的随机端口还在监听。"""
    return port_open(read_bot_port())


def ensure_plugin(napcat_dir: Path) -> None:
    """把控制台插件部署进 NapCat：复制文件 + 白名单补丁 + 启用（全部幂等）。"""
    src_root = Path(getattr(sys, "_MEIPASS", ROOT)) / "napcat-plugin" / PLUGIN_ID
    dst = napcat_dir / "plugins" / PLUGIN_ID
    if not src_root.exists():
        return
    try:
        if not dst.exists() or any(
            (dst / n).read_bytes() != (src_root / n).read_bytes()
            for n in ("index.mjs", "webui/index.html", "package.json")
            if (src_root / n).exists()
        ):
            shutil.copytree(src_root, dst, dirs_exist_ok=True)
    except OSError:
        pass
    # NapCat v4.18+ 非官方插件白名单：把插件名补进 napcat.mjs（原文件备份一次）
    mjs = napcat_dir / "napcat.mjs"
    try:
        text = mjs.read_text(encoding="utf-8", errors="ignore")
        anchor, name = '"napcat-plugin-qce"', f'"{PLUGIN_ID}"'
        if anchor in text and name not in text:
            backup = napcat_dir / "napcat.mjs.bak"
            if not backup.exists():
                shutil.copy2(mjs, backup)
            mjs.write_text(text.replace(anchor, anchor + ", " + name), encoding="utf-8")
    except OSError:
        pass
    # plugins.json 启用
    pj = napcat_dir / "config" / "plugins.json"
    try:
        data = {}
        if pj.exists():
            data = json.loads(pj.read_text(encoding="utf-8"))
        data[PLUGIN_ID] = True
        pj.parent.mkdir(parents=True, exist_ok=True)
        pj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


# ---------------------------------------------------------------- 启动器主体

class Launcher:
    def __init__(self):
        self.cfg = load_config()
        self.proc_napcat: subprocess.Popen | None = None
        self.proc_bot: subprocess.Popen | None = None
        self.status = {"napcat": False, "bot": False, "bot_connected": False}

        self.root = tk.Tk()
        self.root.title("QQ AI 机器人 · 启动器")
        self.root.geometry("680x520")
        self.root.minsize(560, 420)
        self._build_ui()

        threading.Thread(target=self._status_loop, daemon=True).start()
        self.root.after(300, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------

    def _build_ui(self):
        top = tk.Frame(self.root, padx=12, pady=10)
        top.pack(fill="x")

        self.btn_all = tk.Button(top, text="🚀 一键启动", font=("Microsoft YaHei", 12, "bold"),
                                 bg="#2f6fdb", fg="white", width=14, command=self.one_click)
        self.btn_all.pack(side="left", padx=(0, 10))

        tk.Button(top, text="🌐 控制台", width=10, command=lambda: open_url(CONSOLE_URL)).pack(side="left", padx=3)
        tk.Button(top, text="📋 WebUI", width=9, command=self._open_webui).pack(side="left", padx=3)
        tk.Button(top, text="📌 桌面快捷方式", width=13, command=self._make_shortcut).pack(side="right", padx=3)

        st = tk.Frame(self.root, padx=12, pady=4)
        st.pack(fill="x")
        self.lbl_napcat = tk.Label(st, text="⚪ NapCat：检测中…", font=("Microsoft YaHei", 11))
        self.lbl_napcat.pack(side="left", padx=(0, 24))
        self.lbl_bot = tk.Label(st, text="⚪ 机器人：检测中…", font=("Microsoft YaHei", 11))
        self.lbl_bot.pack(side="left")

        ops = tk.Frame(self.root, padx=12, pady=4)
        ops.pack(fill="x")
        self.btn_nc = tk.Button(ops, text="▶ 启动 NapCat", width=13, command=self.start_napcat)
        self.btn_nc.pack(side="left", padx=3)
        tk.Button(ops, text="⏹ 停止 NapCat", width=13, command=self.stop_napcat).pack(side="left", padx=3)
        self.btn_bot = tk.Button(ops, text="▶ 启动机器人", width=13, command=self.start_bot)
        self.btn_bot.pack(side="left", padx=(18, 3))
        tk.Button(ops, text="⏹ 停止机器人", width=13, command=self.stop_bot).pack(side="left", padx=3)

        self.log = scrolledtext.ScrolledText(self.root, font=("Consolas", 9), bg="#111521", fg="#cfe3ff", height=14)
        self.log.pack(fill="both", expand=True, padx=12, pady=(6, 12))

    def log_line(self, msg: str) -> None:
        self.root.after(0, self._append_log, msg)

    def _append_log(self, msg: str) -> None:
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")

    def _drain_log(self) -> None:
        # 由各读输出线程直接调 log_line（经 after 回主线程），此处仅保活
        self.root.after(500, self._drain_log)

    # ---------- 进程管理 ----------

    def _pipe_reader(self, proc: subprocess.Popen, tag: str) -> None:
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log_line(f"{tag}｜{line}")
        except (OSError, ValueError):
            pass
        self.log_line(f"{tag}｜进程已退出（代码 {proc.poll()}）")

    def _spawn(self, args: list, cwd: str, env: dict, tag: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            args, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        threading.Thread(target=self._pipe_reader, args=(proc, tag), daemon=True).start()
        return proc

    def start_napcat(self) -> bool:
        if self.proc_napcat and self.proc_napcat.poll() is None:
            self.log_line("NapCat 已由启动器运行中，跳过")
            return True
        if port_open(NAPCAT_PORT):
            self.log_line(f"端口 {NAPCAT_PORT} 已有 NapCat 在监听（非启动器启动），跳过启动")
            return True
        ensure_plugin(napcat_dir)
        napcat_dir = Path(self.cfg["napcat_dir"])
        boot = napcat_dir / "NapCatWinBootMain.exe"
        qq = find_qq_exe(self.cfg.get("qq_path", ""))
        if not boot.exists():
            messagebox.showerror("启动失败", f"未找到 {boot}\n请在 config.toml [launcher] 里修正 napcat_dir")
            return False
        if not qq:
            messagebox.showerror("启动失败", "未找到 QQ.exe，请在 config.toml [launcher] 里填写 qq_path")
            return False

        main_mjs = str(napcat_dir / "napcat.mjs").replace("\\", "/")
        (napcat_dir / "loadNapCat.js").write_text(
            f'(async () => {{await import("file:///{main_mjs}")}})()', encoding="utf-8"
        )
        env = os.environ.copy()
        env.update({
            "NAPCAT_PATCH_PACKAGE": str(napcat_dir / "qqnt.json"),
            "NAPCAT_LOAD_PATH": str(napcat_dir / "loadNapCat.js"),
            "NAPCAT_INJECT_PATH": str(napcat_dir / "NapCatWinBootHook.dll"),
            "NAPCAT_LAUNCHER_PATH": str(boot),
            "NAPCAT_MAIN_PATH": main_mjs,
        })
        args = [str(boot), qq, str(napcat_dir / "NapCatWinBootHook.dll")]
        if int(self.cfg.get("qq_account") or 0):
            args.append(str(int(self.cfg["qq_account"])))
            self.log_line(f"启动 NapCat（快速登录 {self.cfg['qq_account']}）…")
        else:
            self.log_line("启动 NapCat（未配置 qq_account，将显示二维码扫码登录）…")
        self.proc_napcat = self._spawn(args, str(napcat_dir), env, "NapCat")
        return True

    def stop_napcat(self) -> None:
        if not (self.proc_napcat and self.proc_napcat.poll() is None):
            self.log_line("NapCat 不是本启动器启动的（或未运行），无法停止")
            return
        # /T 连同子进程（含其拉起的 QQ.exe）一起结束，不影响你桌面上登录的其它 QQ
        subprocess.run(["taskkill", "/PID", str(self.proc_napcat.pid), "/T", "/F"],
                       capture_output=True, timeout=15)
        self.log_line("已停止 NapCat（及其拉起的 QQ）")

    def start_bot(self) -> bool:
        if self.proc_bot and self.proc_bot.poll() is None:
            self.log_line("机器人已由启动器运行中，跳过")
            return True
        if bot_alive():
            self.log_line("机器人已在运行（非启动器启动），跳过启动")
            return True
        if not port_open(NAPCAT_PORT):
            self.log_line("提示：NapCat 未运行，机器人将自动重连等待其就绪")
        self.log_line("启动机器人 …")
        if FROZEN:
            # 单文件 exe：再次拉起自身，以 --run-bot 参数进入机器人模式
            args = [sys.executable, "--run-bot"]
        else:
            args = [sys.executable, str(ROOT / "launcher.pyw"), "--run-bot"]
        self.proc_bot = self._spawn(args, str(ROOT), os.environ.copy(), "机器人")
        return True

    def stop_bot(self) -> None:
        if not (self.proc_bot and self.proc_bot.poll() is None):
            self.log_line("机器人不是本启动器启动的（或未运行），无法停止")
            return
        self.proc_bot.terminate()
        self.log_line("已停止机器人")

    def one_click(self) -> None:
        self.btn_all.config(state="disabled")

        def worker():
            try:
                if not self.start_napcat():
                    return
                self.log_line("等待 NapCat 就绪（最多 120 秒）…")
                for _ in range(120):
                    if port_open(NAPCAT_PORT):
                        break
                    time.sleep(1)
                if port_open(NAPCAT_PORT):
                    self.log_line("✓ NapCat 就绪")
                else:
                    self.log_line("⚠ NapCat 未在限时内就绪，仍继续启动机器人（它会自动重连）")
                if not self.start_bot():
                    return
                for _ in range(15):
                    if bot_alive():
                        break
                    time.sleep(1)
                if bot_alive():
                    self.log_line("✓ 机器人就绪，正在打开管理控制台（6099）…")
                    open_url(CONSOLE_URL)
                else:
                    self.log_line("机器人启动中，稍后可在 NapCat WebUI 侧边栏打开「QQ AI 机器人」")
            finally:
                self.root.after(0, lambda: self.btn_all.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 状态刷新 ----------

    def _status_loop(self) -> None:
        while True:
            nap = port_open(NAPCAT_PORT)
            bot_port = read_bot_port()
            bot = port_open(bot_port)
            conn = False
            if bot:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{bot_port}/api/status", timeout=1.5) as r:
                        conn = bool(json.loads(r.read().decode()).get("connected"))
                except (OSError, ValueError, json.JSONDecodeError):
                    conn = False
            self.status = {"napcat": nap, "bot": bot, "bot_connected": conn}
            try:
                self.root.after(0, self._apply_status)
            except RuntimeError:
                return  # 窗口已关闭
            time.sleep(3)

    def _apply_status(self) -> None:
        if self.status["napcat"]:
            self.lbl_napcat.config(text="🟢 NapCat：运行中", fg="#1a9e4b")
        else:
            self.lbl_napcat.config(text="⚪ NapCat：未运行", fg="#888")
        if self.status["bot"] and self.status["bot_connected"]:
            self.lbl_bot.config(text="🟢 机器人：已连接", fg="#1a9e4b")
        elif self.status["bot"]:
            self.lbl_bot.config(text="🟡 机器人：运行中（未连上 NapCat）", fg="#c98a00")
        else:
            self.lbl_bot.config(text="⚪ 机器人：未运行", fg="#888")

    # ---------- 杂项 ----------

    def _open_webui(self) -> None:
        if port_open(WEBUI_PORT):
            open_url("http://127.0.0.1:6099/webui")
        else:
            messagebox.showinfo("提示", "NapCat WebUI 未在运行（6099 端口未开）")

    def _make_shortcut(self) -> None:
        if FROZEN:
            exe, args = sys.executable, ""
        else:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            exe = str(pythonw) if pythonw.exists() else sys.executable
            args = str(ROOT / "launcher.pyw")
        desktop = Path.home() / "Desktop" / "QQ AI 机器人.lnk"
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
            "$s.TargetPath='%s';$s.Arguments='%s';$s.WorkingDirectory='%s';"
            "$s.Description='QQ AI 机器人一键启动';$s.Save()" % (desktop, exe, args, ROOT)
        )
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20)
        if r.returncode == 0:
            messagebox.showinfo("完成", f"已创建桌面快捷方式：\n{desktop}")
        else:
            messagebox.showerror("失败", r.stderr.decode("utf-8", "replace")[:300])

    def _on_close(self) -> None:
        running = any(p and p.poll() is None for p in (self.proc_napcat, self.proc_bot))
        if running:
            ans = messagebox.askyesnocancel(
                "退出方式",
                "停止全部并退出选「是」；\n保持后台运行、仅关闭本窗口选「否」；\n取消则继续使用启动器。"
            )
            if ans is None:
                return
            if ans:
                self.stop_bot()
                self.stop_napcat()
        self.root.destroy()


def main() -> None:
    if "--run-bot" in sys.argv:
        # 单文件 exe 的机器人模式：以自身进程运行机器人主程序
        import asyncio

        import bot as bot_module

        bot_module.setup_logging()
        cfg = bot_module.apply_runtime_overrides(bot_module.load_config(CONFIG_PATH))
        asyncio.run(bot_module.async_main(cfg))
        return
    app = Launcher()
    app.root.mainloop()


if __name__ == "__main__":
    main()
