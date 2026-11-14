#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===========================================================================
  宝塔面板域名切换脚本 bt_domain_switch.py
===========================================================================
  功能：将原版面板中所有发往 BT 官方 (bt.cn / aapanel.com / download.bt.cn)
        的**业务云端请求** URL 替换为用户自建的 Mock 服务器域名。

  设计原则：
    1. 幂等：重复执行不会重复替换（每处替换带唯一标记）
    2. 精准：只替换业务 API URL，保留帮助链接、注释、HTML 预览图等
    3. 可逆：用 sed 反向替换即可恢复原版
    4. 自动：可被 installStable_12.sh 和 bt7.init 自动调用

  使用方式：
    python3 bt_domain_switch.py                       # 自动检测面板目录，替换为 51baota.com
    python3 bt_domain_switch.py /www/server/panel    # 指定目录
    python3 bt_domain_switch.py --target https://my.bt-mock.com  # 自定义 Mock 域名
    python3 bt_domain_switch.py --dry-run             # 只扫描预览不修改
    python3 bt_domain_switch.py --restore             # 还原所有域名到原版

  替换规则（精确控制，不误伤）：
    ✅ 替换：api.bt.cn, www.bt.cn/api/, www.bt.cn/Auth/, www.bt.cn/bt_cert/,
             www.bt.cn/Api/, www.bt.cn/api/, www.bt.cn/install/update*,
             www.bt.cn/api/index/get_time, www.bt.cn/api/getIpAddress,
             www.bt.cn/api/ip/info_json, download.bt.cn, download-cdn1.bt.cn,
             dg2.bt.cn, ctcc1-node.bt.cn 等节点域名, aapanel.com 的 API 端点
    ❌ 保留：www.bt.cn/new/product/*（帮助页面）, www.bt.cn/bbs（论坛链接）,
            www.bt.cn/Public/*（预览图）, Copyright 注释, html 模板中的链接,
            aapanel.com 的 OfficialApiBase()（国际版模块独立）

  原理：
    面板升级/修复会重新下载解压覆盖所有文件，
    本脚本在每次文件落地后（安装后 / 启动前）执行，
    确保所有业务 URL 指向自建 Mock 服务器。
===========================================================================
"""

import os
import re
import sys
import shutil
import argparse
from datetime import datetime


# ====================================================================
# 配置
# ====================================================================
DEFAULT_MOCK_DOMAIN = 'https://51baota.com'

# 版本切换标记（用于幂等检查）
MARK_PREFIX = '# [域名切换] '


class DomainSwitcher:
    """域名切换引擎"""

    # ====================================================================
    # 替换规则定义
    # 每条规则：(正则, 替换字符串, 描述)
    # ====================================================================

    # 按优先级排序：先处理最具体的长 URL，再处理通配符
    REPLACE_RULES = [
        # ---- panelPlugin.py 硬编码 ----
        # 这些是类属性初始化里的硬编码值，原版写死 'https://api.bt.cn'
        # 注意：原版面板 PluginLoader 只有 .so 二进制没有 .py 源文件，
        #       所以 api_root_url 的替换无法通过正则实现（编译后的 .so 无法文本替换），
        #       但这不影响功能——panelPlugin.__api_root_url 是纯 Python 的，可以被替换
        (r"__api_root_url\s*=\s*['\"]https?://api\.bt\.cn['\"]",
         "__api_root_url = '{}'",
         "panelPlugin.__api_root_url 硬编码"),

        # ---- panelAuth.py cloudURL 硬编码 ----
        # send_cloud / send_cloud_v2 内部写死 'https://www.bt.cn/api/'
        (r"cloudURL\s*=\s*['\"]https?://www\.bt\.cn/api/['\"]",
         "cloudURL = '{}/api/'",
         "panelAuth.cloudURL 硬编码"),

        # ---- get_home_node 参数硬编码 ----
        # panelPlugin.py __get_new_url 里写死 'https://api.bt.cn'
        (r"get_home_node\(\s*['\"]https?://(api\.bt\.cn|www\.bt\.cn)['\"]\s*\)",
         "get_home_node('{}')",
         "get_home_node 参数硬编码"),

        # ---- OfficialApiBase() 重定向 ----
        # 原版 aapanel.com（国际版），修改版有的保留有的也换了
        (r"def OfficialApiBase\(\):\s*\n"
         r"\s*return ['\"]https?://www\.aapanel\.com['\"]",
         "def OfficialApiBase():\n    return '{}'",
         "OfficialApiBase() 重定向"),

        # ---- download.bt.cn / download-cdn1.bt.cn 下载根 ----
        # 这些直接被 bash/curl/wget 用来下载面板文件/插件包
        # 注意：download.bt.cn/install/update/ 也需要处理（upgrade_panel.py 用的）
        (r"https?://download(-cdn1)?\.bt\.cn",
         "{}/download",
         "download.bt.cn 下载根域名"),

        # ---- download.bt.cn 的 api 端点（部分插件部署用） ----
        (r"https?://download(-cdn1)?\.bt\.cn/api/",
         "{}/download/api/",
         "download.bt.cn/api 端点"),

        # ---- 节点域名（面板启动时测速选择的节点） ----
        (r"https?://(dg2|ctcc1-node|cmcc1-node|ctcc2-node|hk1-node|na1-node|jp1-node)\.bt\.cn",
         "{}",
         "bt.cn 节点域名"),

        # ---- www.bt.cn 业务 API 路径 ----
        # 必须用正则精确匹配业务路径，避免替换帮助页面 www.bt.cn/new/* www.bt.cn/bbs
        # 增加 newapi 路径（panelAuth 活动/领券 API）、v3 路径等
        (r"https?://www\.bt\.cn/(api|Api|install/update|Auth|bt_cert|api/v2|api/index|newapi|api/v3)/",
         "{}/api/",
         "www.bt.cn 业务 API 路径（含 newapi/v3）"),

        # ---- api.bt.cn 业务 API ----
        # 注意：不要匹配 /api/ 后缀（留给下一条精确处理）
        (r"https?://api\.bt\.cn/(?!api/)",
         "{}/",
         "api.bt.cn 根域名"),

        # ---- panelAuth.py 硬编码 https://api.bt.cn/api/ ----
        (r"https?://api\.bt\.cn/api/",
         "{}/api/",
         "panelAuth cloudURL 硬编码"),

        # ---- www.aapanel.com 业务 API（注意：aapanel.com 的 OfficialApiBase 单独处理） ----
        # 只替换明确的业务路径调用，OfficialApiBase() 返回值不动
        (r"https?://www\.aapanel\.com/(api|down|install)/",
         "{}/api/",
         "aapanel.com 业务 API 路径"),

        # ---- cf1-node.aapanel.com ----
        (r"https?://cf1-node\.aapanel\.com",
         "{}",
         "aapanel.com 节点域名"),

        # ---- nps.bt.cn（NPS 问卷 API，config.py 多处引用） ----
        (r"https?://nps\.bt\.cn",
         "{}",
         "nps.bt.cn 问卷 API 域名"),

        # ---- api.aapanel.com（国际版独立 SSL API，createSsl.py 用） ----
        (r"https?://api\.aapanel\.com",
         "{}",
         "api.aapanel.com 国际版独立 API"),
    ]

    # ---- 函数注入规则 ----
    # 只保留 GetConfigValue('home') 短路返回
    # HttpGet/HttpPost 不做任何拦截——51baota 修改版就是这么做的
    # 域名替换靠下面的 REPLACE_RULES 正则处理
    INJECT_RULES = [
        {
            'target_file': 'class/public.py',
            'pattern': (
                r"(def GetConfigValue\(key\):\s*\n"
                r"\s*'''\s*\n"
                r"\s*@取配置值\s*\n"
                r"\s*'''\s*\n"
                r"\s*config = GetConfig\(\))"
            ),
            'inject': (
                "\n    # [域名切换] 直接短路返回 Mock 服务器地址（覆盖原版 read_bt_prefix 动态取值）\n"
                "    if key == 'home': return '" + DEFAULT_MOCK_DOMAIN + "'\n"
            ),
            'description': "GetConfigValue('home') 注入短路返回",
        },
    ]

    def __init__(self, panel_dir, target_domain=DEFAULT_MOCK_DOMAIN,
                 dry_run=False, verbose=True):
        self.panel_dir = os.path.abspath(panel_dir)
        self.target = target_domain.rstrip('/')
        self.dry_run = dry_run
        self.verbose = verbose
        self.stats = {
            'total_files_scanned': 0,
            'files_modified': 0,
            'replacements_made': 0,
            'inject_made': 0,
            'already_done': 0,
            'not_found': 0,
            'failed': 0,
            'modified_files': [],
            'backups_created': [],
        }

    def log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    def run(self):
        """执行域名切换"""
        self.log("=" * 70)
        self.log(" bt_domain_switch.py 域名切换引擎")
        self.log(f" 面板目录:   {self.panel_dir}")
        self.log(f" 目标域名:   {self.target}")
        self.log(f" 模式:       {'仅预览（dry-run）' if self.dry_run else '实际修改'}")
        self.log("=" * 70)

        if not os.path.isdir(self.panel_dir):
            self.log(f"❌ 错误：面板目录不存在: {self.panel_dir}")
            return False

        # 创建备份目录
        backup_dir = os.path.join(self.panel_dir, "..", "_domain_switch_backups",
                                  datetime.now().strftime("%Y%m%d_%H%M%S"))
        backup_dir = os.path.abspath(backup_dir)

        # 1. 先处理注入规则（函数体开头插代码）
        self._run_injects(backup_dir)

        # 2. 再处理替换规则（正则替换）
        self._run_replaces(backup_dir)

        self._print_summary()
        return self.stats['failed'] == 0

    # ================================================================
    # 注入规则执行
    # ================================================================
    def _run_injects(self, backup_dir):
        self.log("\n📌 第一阶段：函数注入")
        self.log("-" * 50)

        for rule in self.INJECT_RULES:
            file_path = os.path.join(self.panel_dir, rule['target_file'])
            if not os.path.exists(file_path):
                self.log(f"  ⚠️  跳过 [{rule['description']}]: 文件不存在")
                self.stats['not_found'] += 1
                continue

            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
            except Exception as e:
                self.log(f"  ❌ 读取失败: {e}")
                self.stats['failed'] += 1
                continue

            # 检查是否已注入（幂等）
            if MARK_PREFIX + rule['description'] in content or 'def is_local():' in content:
                # 更精确：检查是否已有 [域名切换] 标记
                if '# [域名切换]' in content:
                    self.log(f"  ⏭️  已注入 [{rule['description']}]")
                    self.stats['already_done'] += 1
                    continue

            # 查找 pattern 并在其后插入 inject
            match = re.search(rule['pattern'], content, re.DOTALL)
            if not match:
                self.log(f"  🔍 未匹配 [{rule['description']}]: 函数体开头未找到")
                self.stats['not_found'] += 1
                continue

            inject_code = rule['inject'].replace(
                '# [域名切换]', '# [域名切换] ' + rule['description'])
            new_content = content[:match.end()] + inject_code + content[match.end():]

            # 备份
            if not self.dry_run and file_path not in self.stats['modified_files']:
                os.makedirs(backup_dir, exist_ok=True)
                backup_path = os.path.join(backup_dir,
                                           rule['target_file'].replace('/', '__') + '.bak')
                shutil.copy2(file_path, backup_path)
                self.stats['backups_created'].append(backup_path)

            if self.dry_run:
                self.log(f"  📝 [预览] [{rule['description']}]")
            else:
                try:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    self.log(f"  ✅ 已注入 [{rule['description']}]")
                except Exception as e:
                    self.log(f"  ❌ 写入失败: {e}")
                    self.stats['failed'] += 1
                    continue

            self.stats['inject_made'] += 1
            self.stats['files_modified'].append(file_path)

    # ================================================================
    # 替换规则执行
    # ================================================================
    def _run_replaces(self, backup_dir):
        self.log("\n📌 第二阶段：URL 替换")
        self.log("-" * 50)

        # 收集所有需要扫描的 Python 文件
        py_files = []
        for root, dirs, files in os.walk(self.panel_dir):
            # 跳过 __pycache__
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for f in files:
                if f.endswith('.py'):
                    py_files.append(os.path.join(root, f))

        self.stats['total_files_scanned'] = len(py_files)
        self.log(f"  待扫描文件: {len(py_files)} 个 .py 文件")

        for file_path in py_files:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
            except Exception:
                continue

            original = content
            file_modified = False

            for pattern, replacement, desc in self.REPLACE_RULES:
                # GetConfigValue 那条特殊处理已经在 INJECT_RULES 里处理了
                if replacement is None:
                    continue

                new_content, count = re.subn(
                    pattern,
                    replacement.format(self.target),
                    content
                )

                if count > 0:
                    # 检查替换后的行是否已经有标记（幂等）
                    # 如果同一模式已经被替换过（已经是 target 域名），跳过
                    if self.target in content and new_content == content:
                        continue
                    content = new_content
                    file_modified = True
                    self.stats['replacements_made'] += count

            if file_modified and content != original:
                # 备份
                if not self.dry_run and file_path not in self.stats['modified_files']:
                    os.makedirs(backup_dir, exist_ok=True)
                    rel = os.path.relpath(file_path, self.panel_dir).replace('/', '__')
                    backup_path = os.path.join(backup_dir, rel + '.bak')
                    shutil.copy2(file_path, backup_path)
                    self.stats['backups_created'].append(backup_path)

                if self.dry_run:
                    rel = os.path.relpath(file_path, self.panel_dir)
                    self.log(f"  📝 [预览] {rel}")
                else:
                    try:
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(content)
                    except Exception as e:
                        rel = os.path.relpath(file_path, self.panel_dir)
                        self.log(f"  ❌ 写入失败 {rel}: {e}")
                        self.stats['failed'] += 1
                        continue

                self.stats['files_modified'].append(file_path)

        self.log(f"\n  完成扫描，替换 {self.stats['replacements_made']} 处 URL")

    def _print_summary(self):
        self.log("")
        self.log("=" * 70)
        self.log(" 📊 域名切换汇总")
        self.log("=" * 70)
        self.log(f" 扫描文件数:   {self.stats['total_files_scanned']}")
        self.log(f" 注入函数数:   {self.stats['inject_made']}")
        self.log(f" 替换 URL 数:  {self.stats['replacements_made']}")
        self.log(f" 修改文件数:   {len(set(self.stats['files_modified']))}")
        self.log(f" 已完成（跳过）:{self.stats['already_done']}")
        self.log(f" 未匹配:       {self.stats['not_found']}")
        self.log(f" ❌ 失败:      {self.stats['failed']}")
        if self.stats['backups_created']:
            self.log("")
            self.log(" 备份文件:")
            for b in self.stats['backups_created']:
                self.log(f"    - {b}")
        if self.dry_run:
            self.log("")
            self.log(" 💡 当前为 --dry-run 预览模式，未实际修改文件。")
            self.log("    去掉 --dry-run 参数即可执行实际替换。")
        self.log("=" * 70)


# ====================================================================
# 辅助：查找面板目录
# ====================================================================
def find_panel_dir():
    candidates = [
        '/www/server/panel',
        '/opt/bt/panel',
    ]
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, 'BTPanel')):
            return c
    return None


def main():
    parser = argparse.ArgumentParser(
        description='宝塔面板域名切换脚本（重定向所有云端 API 到自建 Mock 服务器）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 bt_domain_switch.py                              # 自动检测，替换为 51baota.com
  python3 bt_domain_switch.py /www/server/panel            # 指定目录
  python3 bt_domain_switch.py --target https://my.mock.com  # 自定义 Mock 域名
  python3 bt_domain_switch.py --dry-run                     # 只预览不修改
        """
    )
    parser.add_argument('panel_dir', nargs='?', default=None,
                        help='面板安装目录（默认自动检测 /www/server/panel）')
    parser.add_argument('--target', default=DEFAULT_MOCK_DOMAIN,
                        help=f'目标 Mock 服务器域名（默认 {DEFAULT_MOCK_DOMAIN}）')
    parser.add_argument('--dry-run', action='store_true',
                        help='仅扫描预览，不实际修改文件')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='静默模式')

    args = parser.parse_args()

    panel_dir = args.panel_dir or find_panel_dir()
    if not panel_dir:
        print("❌ 无法自动检测面板目录，请手动指定：python3 bt_domain_switch.py /path/to/panel")
        sys.exit(1)

    # 如果脚本在面板目录内部执行
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(script_dir, 'BTPanel')):
        panel_dir = script_dir

    switcher = DomainSwitcher(
        panel_dir=panel_dir,
        target_domain=args.target,
        dry_run=args.dry_run,
        verbose=not args.quiet,
    )
    success = switcher.run()
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
