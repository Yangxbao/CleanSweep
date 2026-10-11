"""只读自检：匹配器单元测试、安全检查用例、注册表枚举、残留扫描、备份导出。

不会删除任何文件或注册表项，可放心随时运行：
  python tools/selftest.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backup, cleaner, leftovers, matcher, scanner, uninstaller, winutil  # noqa: E402

failures = 0


def expect(cond, msg):
    global failures
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        failures += 1


def make_app(name, publisher="", install_location=""):
    a = scanner.InstalledApp(name=name, publisher=publisher, install_location=install_location)
    scanner.fill_derived(a)
    return a


print("== 1. 匹配器 ==")
expect(matcher.name_core("Mozilla Firefox 143.0 (x64 zh-CN)") == "mozillafirefox",
       "name_core 去版本/括号")
expect(matcher.name_core("7-Zip 24.08 (x64)") == "7zip", "name_core 处理 7-Zip")

chrome = make_app("Google Chrome", "Google LLC", r"C:\Program Files\Google\Chrome")
m = matcher.match_candidate("Chrome", chrome)
expect(m and m[0] == matcher.STRONG, f"Chrome -> STRONG（实际 {m}）")
m = matcher.match_candidate("Google", chrome)
expect(m and m[0] in (matcher.STRONG, matcher.MEDIUM), f"Google -> 强/中（实际 {m}）")
m = matcher.match_candidate("ChromeUpdate", chrome)
expect(m and m[0] == matcher.WEAK, f"ChromeUpdate -> WEAK（实际 {m}）")
expect(matcher.match_candidate("Programs", chrome) is None, "通用词不匹配")
expect(matcher.match_candidate("Microsoft", chrome) is None, "通用词文件夹不匹配")

zz = make_app("7-Zip 24.08 (x64)", "Igor Pavlov", r"C:\Program Files\7-Zip")
m = matcher.match_candidate("7-Zip", zz)
expect(m and m[0] == matcher.STRONG, f"7-Zip 文件夹 -> STRONG（实际 {m}）")

print("== 2. 文件系统安全检查 ==")
expect(not cleaner.check_fs_path(r"C:\Windows\System32")[0], "拒绝 C:\\Windows 之下")
expect(not cleaner.check_fs_path(r"C:\Program Files")[0], "拒绝删除 Program Files 根目录")
expect(not cleaner.check_fs_path(r"C:\ProgramData\Microsoft\AnyThing")[0], "拒绝 microsoft 路径段")
expect(not cleaner.check_fs_path(r"C:\ProgramData\Package Cache\x")[0], "拒绝 Package Cache")
expect(not cleaner.check_fs_path(r"C:\Users\MR\Desktop\x")[0], "拒绝用户目录")
expect(not cleaner.check_fs_path(r"D:\Anywhere\x")[0], "拒绝未知盘符目录")
expect(cleaner.check_fs_path(r"C:\Program Files\SomeApp")[0], "允许 Program Files\\SomeApp")
expect(not cleaner.check_fs_path(r"C:\Program Files\SomeApp\Windows")[0], "拒绝名为 Windows 的子路径")
lnk = os.path.join(winutil.expand_env(
    r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"), "SomeApp.lnk")
expect(cleaner.check_fs_path(lnk)[0], "允许开始菜单 .lnk")
# 自定义盘符：软件自身安装目录加入动态白名单后放行，盘符根始终拒绝
extra = cleaner.extra_exact_paths_for(make_app("Demo", "", r"E:\7-Zip"))
expect(cleaner.check_fs_path(r"E:\7-Zip", extra)[0], "动态白名单放行 E:\\7-Zip")
expect(not cleaner.check_fs_path(r"E:\Other", extra)[0], "E 盘其他目录仍被拒绝")
extra2 = cleaner.extra_exact_paths_for(make_app("Demo", "", r"E:\Apps\Demo"))
expect(cleaner.check_fs_path(r"E:\Apps\Demo", extra2)[0], "放行 E:\\Apps\\Demo")
expect(cleaner.check_fs_path(r"E:\Apps", extra2)[0], "厂商父目录 E:\\Apps 也在精确白名单")
expect(not cleaner.check_fs_path(r"E:\Apps\Other", extra2)[0], "父目录之下其他内容不放行")

print("== 3. 注册表安全检查 ==")
expect(cleaner.check_reg_path(r"HKLM\SOFTWARE\Google")[0], "允许 HKLM\\SOFTWARE\\Google")
expect(cleaner.check_reg_path(r"HKCU\Software\Google\Chrome")[0], "允许 HKCU 二级键")
expect(not cleaner.check_reg_path(r"HKLM\SOFTWARE\Microsoft\Windows")[0], "拒绝 Microsoft\\Windows")
expect(not cleaner.check_reg_path(r"HKCU\Software")[0], "拒绝删除 Software 根键")
expect(not cleaner.check_reg_path(r"HKCU\Software\Classes")[0], "拒绝 Classes")
expect(not cleaner.check_reg_path(r"HKCR\X")[0], "拒绝 HKCR")
expect(cleaner.check_reg_path(
    r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{12345678-1234-1234-1234-123456789012}")[0],
    "允许卸载条目（白名单）")

print("== 4. 注册表枚举（真实环境，只读） ==")
apps = scanner.enumerate_apps()
expect(len(apps) > 0, f"枚举到 {len(apps)} 个已安装软件")
by_size = [a for a in apps if a.estimated_size_kb]
print(f"    示例：{[a.name for a in apps[:8]]}")
print(f"    带大小的条目 {len(by_size)} 个；MSI 条目 {sum(1 for a in apps if a.is_msi)} 个")
expect(all(a.reg_subkey and a.hive_name for a in apps), "每条记录都有注册表位置")
expect(all(a.name_tokens or a.install_tokens for a in apps), "每条记录都有特征词")

print("== 5. 残留扫描（真实环境，只读） ==")
if apps:
    target = next((a for a in apps if "7-zip" in a.name.lower()), apps[0])
    ctx = leftovers.ScanContext()
    level, reasons = leftovers.assess_risk(target, apps, ctx)
    print(f"    评估对象：{target.name} -> 风险 {level}")
    for r in reasons:
        print(f"      - {r}")
    fs = leftovers.scan_filesystem(target, after_uninstall=True, ctx=ctx,
                                   other_apps=[a for a in apps if a is not target])
    reg = leftovers.scan_registry(target, ctx=ctx,
                                  other_apps=[a for a in apps if a is not target])
    print(f"    文件系统候选 {len(fs)} 项 / 注册表候选 {len(reg)} 项（仅供演示，未删除）")
    for x in (fs + reg)[:6]:
        print(f"      [{x.strength}] {x.path}  {x.matched_by}")
    expect(all(cleaner.check_fs_path(x.path, cleaner.extra_exact_paths_for(target))[0]
               for x in fs if x.kind != "regkey"),
           "扫描出的文件系统候选全部通过安全检查")
    expect(all(cleaner.check_reg_path(x.path)[0] for x in reg if x.kind == "regkey"),
           "扫描出的注册表候选全部通过安全检查")

print("== 6. 注册表导出备份（只读导出，写入临时目录） ==")
tmp = tempfile.mkdtemp(prefix="cleansweep_selftest_")
ok, msg = backup.export_registry_key(r"HKCU\Environment", os.path.join(tmp, "env.reg"))
expect(ok and os.path.isfile(os.path.join(tmp, "env.reg")), f"导出 HKCU\\Environment（{msg}）")
expect(not backup.export_registry_key(r"HKLM\SOFTWARE\不存在的键测试",
                                      os.path.join(tmp, "no.reg"))[0], "导出不存在的键应失败")

print("== 7. 进程工具（只读，不会 taskkill 任何进程） ==")
procs = winutil.list_running_processes()
expect(len(procs) > 10, f"tasklist 枚举到 {len(procs)} 个进程")
expect(any(p["name"] == "explorer.exe" for p in procs), "找到 explorer.exe（只读查找）")
expect(winutil.is_protected_process("explorer.exe")
       and winutil.is_protected_process("svchost.exe")
       and winutil.is_protected_process("msedge.exe")
       and winutil.is_protected_process("cleansweep.exe"),
       "系统进程/浏览器/本工具自身均在保护名单")
expect(winutil.kill_processes({"explorer.exe", "svchost.exe"}) == [],
       "保护名单内的进程不会被强杀（find 即过滤）")
expect(winutil.find_processes(["explorer.exe"]) == [],
       "find_processes 也排除保护名单")
if apps:
    target = next((a for a in apps if "7-zip" in a.name.lower()), apps[0])
    others = [a for a in apps if a is not target]
    names = uninstaller.derive_process_names(target, others)
    print(f"    {target.name} 的卸载相关进程名候选：{sorted(names) or '（无）'}")
    expect(all(not winutil.is_protected_process(n) for n in names),
           "推导出的进程名候选不含受保护进程")

print()
if failures:
    print(f"自检失败：{failures} 项未通过")
    sys.exit(1)
print("自检全部通过 ✓（未删除任何内容）")
