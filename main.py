"""CleanSweep 彻底卸载助手 — 程序入口。

用法：
  python main.py              # 首选：自动请求管理员权限（UAC）
  python main.py --no-elevate # 不请求提权，以受限模式运行
  python main.py --smoke      # 自检模式：加载软件列表后自动退出（不弹窗）
"""
import sys


def main() -> int:
    argv = sys.argv[1:]
    smoke = "--smoke" in argv
    no_elevate = "--no-elevate" in argv

    if sys.platform != "win32":
        print("本工具仅支持 Windows。")
        return 1

    from app import winutil

    if not (smoke or no_elevate or winutil.is_admin()):
        choice = winutil.message_box(
            "CleanSweep 需要管理员权限才能：\n"
            "  • 清理 HKLM 注册表残留\n"
            "  • 删除 Program Files / ProgramData 中的残留文件\n"
            "  • 创建系统还原点\n\n"
            "是否以管理员身份重新启动？\n（选择“否”将以受限模式继续，部分功能不可用）",
            "CleanSweep 彻底卸载助手",
            winutil.MB_YESNO | winutil.MB_ICONQUESTION)
        if choice == winutil.IDYES and winutil.relaunch_as_admin(""):
            return 0  # 新进程已启动，当前进程退出

    from app.gui import run
    run(smoke=smoke)
    return 0


if __name__ == "__main__":
    sys.exit(main())
