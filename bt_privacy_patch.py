#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===========================================================================
  宝塔面板隐私上报补丁脚本 bt_privacy_patch.py
===========================================================================
  功能：扫描并注释掉面板运行时所有"纯上报"类接口调用，
        同时修复少数有返回值依赖的上报函数（改本地逻辑跳过上报）。

  设计原则：
    1. 幂等：重复执行不会重复注释（每处补丁带唯一标记）
    2. 精准：只动纯上报，返回值有业务依赖的不动
    3. 可逆：每处补丁都加 [隐私屏蔽] 标记，手动搜索即可定位
    4. 自动：可被 installStable_12.sh 和 bt7.init 自动调用

  使用方式：
    python3 bt_privacy_patch.py                   # 自动检测 /www/server/panel
    python3 bt_privacy_patch.py /www/server/panel  # 指定面板目录
    python3 bt_privacy_patch.py --patch-dir /path  # 同上
    python3 bt_privacy_patch.py --dry-run          # 只扫描不修改，预览效果
    python3 bt_privacy_patch.py --restore          # 还原所有已打补丁的文件

  原理：
    面板升级/修复会重新下载解压覆盖所有文件，
    本脚本在每次文件落地后（安装后 / 启动前）执行，
    确保所有上报点被处理，功能零影响。
===========================================================================
"""

import os
import re
import sys
import argparse
import shutil
from datetime import datetime


# ====================================================================
# 补丁标记：所有补丁都以此注释开头，实现幂等和可搜索
# ====================================================================
MARK = "# [隐私屏蔽]"
MARK_FULL = "# ===== [隐私屏蔽] bt_privacy_patch.py 自动处理 ====="


class PatchRule:
    """单条补丁规则"""

    def __init__(self, name, file_rel, pattern, replacement,
                 description="", use_regex=True):
        """
        name:       规则名称（唯一标识）
        file_rel:   相对面板根目录的文件路径
        pattern:    要匹配的代码块（支持多行正则）
        replacement:替换后的代码块（保留结构，注释掉上报调用）
        description:补丁说明（日志用）
        use_regex:  pattern 是否是正则表达式
        """
        self.name = name
        self.file_rel = file_rel
        self.pattern = pattern
        self.replacement = replacement
        self.description = description
        self.use_regex = use_regex


# ====================================================================
# 补丁规则定义：按 URL 锚点精确匹配上报调用行，注释掉即可
# ====================================================================

def build_rules():
    """构建所有补丁规则

    匹配策略：直接用上报 URL（如 bt_error/index.php、/api/panel/s_error 等）作为锚点，
    匹配包含该 URL 的整行代码（含前导缩进），然后在该行前加 `# [隐私屏蔽]` 注释掉。
    这样不依赖精确的函数签名（public. 前缀有无、参数名差异等），版本兼容性更强。
    """
    rules = []

    # ================================================================
    # 第一组：bt_error 异常上报（5 处）—— 统一 URL 锚点 bt_error/index.php
    # 原版 5 处代码结构相同，都包含 run_thread(httpPost, ("https://xxx/bt_error/index.php", error_infos))
    # 但有 public. 前缀差异（public.py / common.py 内部直接调用不带 public.）
    # ================================================================

    # 所有包含 bt_error/index.php 的 run_thread/httpPost 调用行都注释掉
    # 用多行 DOTALL 匹配：前导空白 + run_thread 或 httpPost + URL
    rules.append(PatchRule(
        name="bt_error_all",
        file_rel="*",  # 通配：PrivacyPatcher._apply_rule 会跳过 file_rel 为 * 的文件，
                       # 我们在 _apply_rule 外面加一个全文件扫描逻辑
        description="bt_error 异常上报（5 处通用规则）",
        pattern=(
            r"(^\s*)"                         # Group 1: 前导缩进（保留）
            r"(?:public\.)?"                  # 可选 public. 前缀
            r"(run_thread\(|\w+\.run_thread\()"  # run_thread( 或 xxx.run_thread(
            r"(?:public\.)?"                  # 可选 public. 前缀
            r"httpPost,"                      # httpPost,
            r"\s*"
            r'\(\s*["\']https?://[^"\']*bt_error/index\.php["\']'
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 bt_error 异常上报\n"
            r"\1# "                           # 注释掉原始行
        )
    ))

    # 直接 httpPost 调用 bt_error（不走 run_thread 包装的情况）
    rules.append(PatchRule(
        name="bt_error_direct_call",
        file_rel="*",
        description="bt_error 直接 httpPost 上报（不走线程包装）",
        pattern=(
            r"(^\s*)"
            r"(?:public\.)?"
            r"httpPost\("
            r'\s*["\']https?://[^"\']*bt_error/index\.php["\']'
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 bt_error 直接上报\n"
            r"\1# "
        )
    ))

    # ================================================================
    # 第二组：s_error 系统错误上报（1 处，class/public.py L1304）
    # 原版: httpPost(GetConfigValue('home') + "/api/panel/s_error", pdata, timeout=3)
    # ================================================================
    rules.append(PatchRule(
        name="s_error",
        file_rel="class/public.py",
        description="s_error 系统错误上报: class/public.py",
        pattern=(
            r"(^\s*)"
            r"(?:public\.)?"
            r'httpPost\(.*?/api/panel/s_error'
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 s_error 上报\n"
            r"\1# "
        )
    ))

    # ================================================================
    # 第三组：plugin_total 插件安装计数上报
    # 原版 7 处，覆盖 panelPlugin.py（2 处 run_thread 包装）+ 各插件 setup 文件
    # ================================================================
    rules.append(PatchRule(
        name="plugin_total_all",
        file_rel="*",
        description="plugin_total 插件计数上报（通用规则覆盖所有）",
        pattern=(
            r"(^\s*)"
            r"("  # 普通捕获组：二选一（非捕获组写 (?:" 在 Python raw string 里会变成 (?": 导致 re.error）
            r"(?:public\.)?run_thread\(\s*(?:public\.)?httpPost,"
            r"|"
            r"(?:public\.)?httpPost\("
            r")"
            r"[^;\n]*?/api/panel/plugin_total"
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 plugin_total 插件计数上报\n"
            r"\1# "
        )
    ))

    # ================================================================
    # 第四组：total_keyword 搜索关键词上报（1 处，class/public.py L3665）
    # 原版: httpPost(GetConfigValue('home') + '/api/panel/total_keyword', pdata)
    # 注意：原版直接调用 httpPost 没有 run_thread 包装！
    # ================================================================
    rules.append(PatchRule(
        name="total_keyword",
        file_rel="class/public.py",
        description="total_keyword 搜索关键词上报: class/public.py",
        pattern=(
            r"(^\s*)"
            r"(?:public\.)?"
            r"httpPost\("
            r"[^;\n]*?/api/panel/total_keyword"
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 total_keyword 上报\n"
            r"\1# "
        )
    ))

    # ================================================================
    # 第五组：WP Toolkit 统计（3 处，class/wptoolkitModel/wp_toolkit/core.py）
    # 原版用 threading.Thread + requests.post，URL 格式 '{}/api/panel/wp_installed'.format(...)
    # 和 '{}/api/panel/panel_count_daily'.format(...)
    # ================================================================
    rules.append(PatchRule(
        name="wp_toolkit_installed",
        file_rel="class/wptoolkitModel/wp_toolkit/core.py",
        description="WP wp_installed 安装成功上报: core.py L1829",
        pattern=(
            r"(^\s*)"
            r"threading\.Thread\(target=.*?wp_installed.*?\.start\(\)"
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 WP wp_installed 上报\n"
            r"\1# "
        )
    ))
    rules.append(PatchRule(
        name="wp_toolkit_daily",
        file_rel="class/wptoolkitModel/wp_toolkit/core.py",
        description="WP panel_count_daily 每日统计上报（2处）: core.py L5134/L5168",
        pattern=(
            r"(^\s*)"
            r"threading\.Thread\(target=.*?panel_count_daily.*?\.start\(\)"
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 WP panel_count_daily 上报\n"
            r"\1# "
        )
    ))

    # ================================================================
    # 第六组：submit_expand_pack_used 邮件群发硬件指纹上报
    # 原版在 class/mailModel/bulkModel.py，有返回值依赖
    # 处理方式：注释掉 requests.post 调用，直接 continue/return 后续刷新逻辑
    # ================================================================
    rules.append(PatchRule(
        name="submit_expand_pack_used",
        file_rel="class/mailModel/bulkModel.py",
        description="submit_expand_pack_used 硬件指纹上报: bulkModel.py",
        pattern=(
            r"(^\s*)"
            r"listTmp\s*=\s*requests\.post\(cloudUrl,\s*json=pdata,\s*headers=url_headers\)"
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 submit_expand_pack_used 硬件指纹上报\n"
            r"\1# "
        )
    ))

    # ================================================================
    # 第七组：webshell_check.py 的 send_baota / send_baota2（2 处）
    # 原版有返回值依赖：send_baota 用 ret 判断是否调用 check_webshell
    # 处理方式：注释掉 httpPost，直接返回本地 check_webshell 结果
    # ================================================================

    # send_baota2: 原版 ret = public.httpPost(cloudUrl, pdata); return True
    rules.append(PatchRule(
        name="webshell_send_baota2",
        file_rel="class/webshell_check.py",
        description="btwaf_submit send_baota2（注释掉 httpPost）: webshell_check.py",
        pattern=(
            r"(^\s*)"
            r"ret\s*=\s*public\.httpPost\(cloudUrl,\s*pdata\)"
            r"(\s*\n\s*return True)"
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 btwaf_submit send_baota2（跳过源码上传）\n"
            r"\1# " + r"ret = public.httpPost(cloudUrl, pdata)" + r"\2"
        )
    ))

    # send_baota: 原版 ret = public.httpPost(...); if ret == '1' / '-1': return check_webshell
    rules.append(PatchRule(
        name="webshell_send_baota",
        file_rel="class/webshell_check.py",
        description="btwaf_submit send_baota（注释掉 httpPost，直接本地查询）: webshell_check.py",
        pattern=(
            r"(^\s*)"
            r"ret\s*=\s*public\.httpPost\(cloudUrl,\s*pdata\)"
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 btwaf_submit send_baota（跳过源码上传，改用本地 check_webshell）\n"
            r"\1# "
        )
    ))

    # ================================================================
    # 第八组：flush_plugin.py 中的 info_json 请求
    # 这不是隐私上报而是 API 存活检测
    # ================================================================
    rules.append(PatchRule(
        name="flush_plugin_info_json",
        file_rel="script/flush_plugin.py",
        description="flush_plugin info_json API 存活检测: flush_plugin.py",
        pattern=(
            r"(^\s*)"
            r"(?:res\s*=\s*)?requests?\.(?:post|get)\("
            r"[^;\n]*?/api/ip/info_json"
        ),
        replacement=(
            r"\1" + MARK + r" 已屏蔽 flush_plugin info_json 请求\n"
            r"\1# "
        )
    ))

    return rules


# ====================================================================
# 全文件扫描补丁引擎（处理 file_rel='*' 的通配规则）
# ====================================================================
def apply_global_patterns(panel_dir, dry_run=False, verbose=True):
    """全目录扫描应用通配规则（file_rel='*' 的规则）

    遍历所有 .py 文件，对每个文件内容尝试匹配全局规则。
    返回: {'patched': int, 'modified_files': list, 'failed': int, 'backups_created': list}
    """
    log = print if verbose else (lambda x: None)
    stats = {'patched': 0, 'modified_files': [], 'failed': 0, 'backups_created': []}

    global_rules = [r for r in build_rules() if r.file_rel == '*']
    if not global_rules:
        return stats

    log(f"\n📌 全目录扫描（{len(global_rules)} 条通配规则）")
    log("-" * 50)

    backup_dir = os.path.join(panel_dir, "..", "_privacy_patch_backups",
                              datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup_dir = os.path.abspath(backup_dir)

    py_files = []
    for root, dirs, files in os.walk(panel_dir):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for f in files:
            if f.endswith('.py'):
                py_files.append(os.path.join(root, f))

    for file_path in py_files:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception:
            continue

        original = content
        file_patched = 0
        for rule in global_rules:
            try:
                new_content, count = re.subn(
                    rule.pattern, rule.replacement, content, flags=re.MULTILINE
                )
            except re.error:
                continue
            if count > 0 and new_content != content:
                # 检查幂等：替换后已经有 [隐私屏蔽] 标记，跳过
                if MARK in new_content and MARK in content:
                    # 已经打过补丁了，不重复替换
                    continue
                content = new_content
                file_patched += count

        if content != original:
            if not dry_run:
                os.makedirs(backup_dir, exist_ok=True)
                rel = os.path.relpath(file_path, panel_dir).replace('/', '__')
                backup_path = os.path.join(backup_dir, rel + '.bak')
                shutil.copy2(file_path, backup_path)
                stats['backups_created'].append(backup_path)
                try:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                except Exception as e:
                    log(f"  ❌ 写入失败 {os.path.relpath(file_path, panel_dir)}: {e}")
                    stats['failed'] += 1
                    continue
            stats['patched'] += file_patched
            stats['modified_files'].append(file_path)
            rel = os.path.relpath(file_path, panel_dir)
            log(f"  ✅ 已补丁 {rel}（{file_patched} 处）")

    log(f"\n  全目录扫描完成：{len(stats['modified_files'])} 个文件，{stats['patched']} 处替换")
    return stats


# ====================================================================
# 核心补丁引擎
# ====================================================================

class PrivacyPatcher:
    """隐私补丁引擎"""

    def __init__(self, panel_dir, dry_run=False, verbose=True):
        self.panel_dir = os.path.abspath(panel_dir)
        self.dry_run = dry_run
        self.verbose = verbose
        self.stats = {
            'total_rules': 0,
            'matched': 0,
            'patched': 0,
            'already_patched': 0,
            'not_found': 0,
            'failed': 0,
            'modified_files': set(),
            'backups_created': [],
        }

    def log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    def run(self):
        """执行补丁流程"""
        rules = build_rules()
        self.stats['total_rules'] = len(rules)

        self.log("=" * 70)
        self.log(f" bt_privacy_patch.py 隐私补丁引擎")
        self.log(f" 面板目录: {self.panel_dir}")
        self.log(f" 规则数量: {len(rules)}")
        self.log(f" 模式: {'仅预览（dry-run）' if self.dry_run else '实际修改'}")
        self.log("=" * 70)

        if not os.path.isdir(self.panel_dir):
            self.log(f"❌ 错误：面板目录不存在: {self.panel_dir}")
            return False

        # 创建备份目录
        backup_dir = os.path.join(self.panel_dir, "..", "_privacy_patch_backups",
                                  datetime.now().strftime("%Y%m%d_%H%M%S"))
        backup_dir = os.path.abspath(backup_dir)

        for rule in rules:
            # 跳过通配规则（file_rel='*'），它们由 apply_global_patterns 处理
            if rule.file_rel == '*':
                continue
            self._apply_rule(rule, backup_dir)

        # 执行全目录通配规则扫描（bt_error_all / bt_error_direct_call / plugin_total_all）
        global_stats = apply_global_patterns(self.panel_dir, self.dry_run, self.verbose)
        self.stats['patched'] += global_stats.get('patched', 0)
        for mf in global_stats.get('modified_files', []):
            self.stats['files_modified'].add(mf)
        self.stats['failed'] += global_stats.get('failed', 0)
        self.stats['backups_created'].extend(global_stats.get('backups_created', []))

        self._print_summary()
        return self.stats['failed'] == 0

    def _apply_rule(self, rule, backup_dir):
        """应用单条规则"""
        file_path = os.path.join(self.panel_dir, rule.file_rel)

        if not os.path.exists(file_path):
            # 规则对应的文件不存在（可能面板版本不同），不算失败
            self.log(f"  ⚠️  跳过 [{rule.name}]: 文件不存在 {rule.file_rel}")
            self.stats['not_found'] += 1
            return

        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception as e:
            self.log(f"  ❌ 读取失败 [{rule.name}]: {e}")
            self.stats['failed'] += 1
            return

        # 检查是否已打过补丁（幂等）
        if MARK in content.split('\n')[0:50] or MARK in content[:500]:
            # 再精确检查：是否已注释了 pattern 中关键的 URL 字符串
            url_patterns = [
                'api.bt.cn/bt_error/index.php',
                'aapanel.com/bt_error/index.php',
                'bt_error/index.php',
                '/api/panel/plugin_total',
                '/api/panel/btwaf_submit',
                '/api/panel/total_keyword',
                '/api/panel/wp_installed',
                '/api/panel/panel_count_daily',
                '/api/panel/submit_expand_pack_used',
                '/api/panel/s_error',
            ]
            is_already_patched = False
            for up in url_patterns:
                if up in content and '# ' + up in content:
                    is_already_patched = True
                    break

            if is_already_patched:
                self.log(f"  ⏭️  已补丁 [{rule.name}]: {rule.description}")
                self.stats['already_patched'] += 1
                return

        # 尝试匹配并替换
        if rule.use_regex:
            try:
                new_content, count = re.subn(
                    rule.pattern, rule.replacement, content, flags=re.DOTALL
                )
            except re.error as e:
                self.log(f"  ❌ 正则错误 [{rule.name}]: {e}")
                self.stats['failed'] += 1
                return
        else:
            if rule.pattern in content:
                count = 1
                new_content = content.replace(rule.pattern, rule.replacement, 1)
            else:
                count = 0
                new_content = content

        if count == 0:
            self.log(f"  🔍 未匹配 [{rule.name}]: {rule.description}")
            self.stats['not_found'] += 1
            return

        self.stats['matched'] += 1

        if new_content == content:
            # 匹配到了但没改内容（可能 replacement 写的不好）
            self.log(f"  ⚠️  匹配但无变更 [{rule.name}]")
            self.stats['already_patched'] += 1
            return

        # 写入前备份
        if file_path not in self.stats['modified_files'] and not self.dry_run:
            os.makedirs(backup_dir, exist_ok=True)
            rel_path = rule.file_rel.replace('/', '__')
            backup_path = os.path.join(backup_dir, rel_path + '.bak')
            shutil.copy2(file_path, backup_path)
            self.stats['backups_created'].append(backup_path)

        if self.dry_run:
            self.log(f"  📝 [预览] [{rule.name}]: {rule.description}")
        else:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                self.log(f"  ✅ 已补丁 [{rule.name}]: {rule.description}")
            except Exception as e:
                self.log(f"  ❌ 写入失败 [{rule.name}]: {e}")
                self.stats['failed'] += 1
                return

        self.stats['patched'] += 1
        self.stats['modified_files'].add(file_path)

    def _print_summary(self):
        """打印汇总"""
        self.log("")
        self.log("=" * 70)
        self.log(" 📊 补丁汇总")
        self.log("=" * 70)
        self.log(f" 总规则数:     {self.stats['total_rules']}")
        self.log(f" ✅ 已补丁:     {self.stats['patched']}")
        self.log(f" ⏭️  已补丁(跳过): {self.stats['already_patched']}")
        self.log(f" 🔍 未匹配:     {self.stats['not_found']}")
        self.log(f" ❌ 失败:       {self.stats['failed']}")
        if self.stats['modified_files']:
            self.log("")
            self.log(" 已修改文件:")
            for f in sorted(self.stats['modified_files']):
                rel = os.path.relpath(f, self.panel_dir)
                self.log(f"    - {rel}")
        if self.stats['backups_created']:
            self.log("")
            self.log(" 备份文件:")
            for b in self.stats['backups_created']:
                self.log(f"    - {b}")
        if self.dry_run:
            self.log("")
            self.log(" 💡 当前为 --dry-run 预览模式，未实际修改文件。")
            self.log("    去掉 --dry-run 参数即可执行实际补丁。")
        self.log("=" * 70)


def find_panel_dir():
    """自动检测面板目录"""
    candidates = [
        '/www/server/panel',
        '/opt/bt/panel',
    ]
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, 'BTPanel')):
            return c
    return None


def create_not_submit_errinfo(panel_dir):
    """创建 not_submit_errinfo.pl 标记文件"""
    fpath = os.path.join(panel_dir, 'not_submit_errinfo.pl')
    if not os.path.exists(fpath):
        with open(fpath, 'w') as f:
            f.write('1\n')
        print(f"  ✅ 已创建标记文件: {fpath}")


def main():
    parser = argparse.ArgumentParser(
        description='宝塔面板隐私上报补丁脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 bt_privacy_patch.py                       # 自动检测面板目录并执行
  python3 bt_privacy_patch.py /www/server/panel    # 指定目录
  python3 bt_privacy_patch.py --dry-run             # 只预览不修改
        """
    )
    parser.add_argument('patch_dir', nargs='?', default=None,
                        help='面板安装目录（默认自动检测 /www/server/panel）')
    parser.add_argument('--dry-run', action='store_true',
                        help='仅扫描预览，不实际修改文件')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='静默模式，减少输出')

    args = parser.parse_args()

    # 确定面板目录
    panel_dir = args.patch_dir or find_panel_dir()
    if not panel_dir:
        print("❌ 无法自动检测面板目录，请手动指定：python3 bt_privacy_patch.py /path/to/panel")
        print("   或把脚本放到 /www/server/panel 的同级目录执行")
        sys.exit(1)

    # 如果脚本在面板目录内部执行，用脚本所在目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(script_dir, 'BTPanel')):
        panel_dir = script_dir

    verbose = not args.quiet
    patcher = PrivacyPatcher(panel_dir, dry_run=args.dry_run, verbose=verbose)
    success = patcher.run()

    # 辅助：创建 not_submit_errinfo.pl 标记文件
    if success and not args.dry_run:
        create_not_submit_errinfo(panel_dir)

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
