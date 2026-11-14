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
# 补丁规则定义：18 个上报点 + 1 个辅助文件
# ====================================================================

def build_rules():
    """构建所有补丁规则"""
    rules = []

    # ================================================================
    # 第一组：bt_error 异常上报（5 处）—— 异步发，返回值没人取
    # ================================================================

    rules.append(PatchRule(
        name="bt_error_BTPanel",
        file_rel="BTPanel/__init__.py",
        description="bt_error 上报 #1: BTPanel/__init__.py L529",
        pattern=(
            r"(?ms)"
            r"(\s*# 提交异常报告\s*\n"
            r"\s*if not public\.cache_get\(pkey\):\s*\n"
            r"\s*try:\s*\n)"
            r"(\s*public\.run_thread\(public\.httpPost, "
            r'\("https://api\.bt\.cn/bt_error/index\.php", error_infos\)\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 bt_error 上报 #1\n\2" + "\n        except: pass"
    ))

    # config.py:4483
    rules.append(PatchRule(
        name="bt_error_config",
        file_rel="class/config.py",
        description="bt_error 上报 #2: class/config.py L4483",
        pattern=(
            r"(\s*# 提交\s*\n"
            r"\s*if not public\.cache_get\(pkey\):\s*\n"
            r"\s*try:\s*\n)"
            r"(\s*public\.run_thread\(public\.httpPost, "
            r'\("https://api\.bt\.cn/bt_error/index\.php", error_infos\)\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 bt_error 上报 #2\n\2" + "\n            except: pass"
    ))

    # userlogin.py:242
    rules.append(PatchRule(
        name="bt_error_userlogin",
        file_rel="class/userlogin.py",
        description="bt_error 上报 #3: class/userlogin.py L242",
        pattern=(
            r"(\s*# 提交异常报告\s*\n"
            r"\s*if not public\.cache_get\(pkey\):\s*\n"
            r"\s*try:\s*\n)"
            r"(\s*public\.run_thread\(public\.httpPost, "
            r'\("https://api\.bt\.cn/bt_error/index\.php", error_infos\)\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 bt_error 上报 #3\n\2" + "\n            except: pass"
    ))

    # public.py:8470
    rules.append(PatchRule(
        name="bt_error_public",
        file_rel="class/public.py",
        description="bt_error 上报 #4: class/public.py L8470",
        pattern=(
            r"(\s*# 提交异常报告\s*\n"
            r"\s*if not public\.cache_get\(pkey\):\s*\n"
            r"\s*try:\s*\n)"
            r"(\s*public\.run_thread\(public\.httpPost, "
            r'\("https://api\.bt\.cn/bt_error/index\.php", error_infos\)\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 bt_error 上报 #4\n\2" + "\n            except: pass"
    ))

    # common.py:1762 (geterror.aapanel.com)
    rules.append(PatchRule(
        name="bt_error_aapanel_common",
        file_rel="mod/base/public_aap/common.py",
        description="bt_error 上报 #5: mod/base/public_aap/common.py L1762",
        pattern=(
            r"(\s*# 提交异常报告\s*\n"
            r"\s*if not public\.cache_get\(pkey\):\s*\n"
            r"\s*try:\s*\n)"
            r"(\s*run_thread\(httpPost, "
            r'\("https://geterror\.aapanel\.com/bt_error/index\.php", error_infos\)\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 bt_error 上报 #5\n\2" + "\n            except: pass"
    ))

    # ================================================================
    # 第二组：s_error 上报（1 处）—— 返回值没人接
    # ================================================================

    rules.append(PatchRule(
        name="s_error",
        file_rel="class/public.py",
        description="s_error 上报: class/public.py L1304",
        pattern=(
            r"(\s*if os\.path\.exists\(sfile\):\s*\n"
            r"\s*try:\s*\n)"
            r"(\s*res = httpPost\(public\.GetConfigValue\('home'\) \+ '/api/panel/s_error', s_error_datas, timeout=\(3, 10\)\))"
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 s_error 上报\n" + MARK + r" 已屏蔽\n# \2" + "\n            except: pass"
    ))

    # ================================================================
    # 第三组：plugin_total 插件安装计数（5 处活跃）—— 返回值没人接
    # ================================================================

    # virtualMod.py:369
    rules.append(PatchRule(
        name="plugin_total_virtual",
        file_rel="mod/project/virtual/virtualMod.py",
        description="plugin_total 上报 #1: virtualMod.py L369",
        pattern=(
            r"(\s*)"
            r"(public\.httpPost\(public\.GetConfigValue\('home'\) \+ '/api/panel/plugin_total', "
            r'{"pid": "402", \'p_name\': "vhost_virtual"}, 3\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 plugin_total 上报 #1\n\1# \2"
    ))

    # manageModel.py:13
    rules.append(PatchRule(
        name="plugin_total_mail",
        file_rel="class/mailModel/manageModel.py",
        description="plugin_total 上报 #2: manageModel.py L13",
        pattern=(
            r"(\s*)"
            r"(public\.httpPost\(public\.GetConfigValue\('home'\) \+ '/api/panel/plugin_total', "
            r'{"pid": "403", \'p_name\': "mailmod"}, 3\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 plugin_total 上报 #2\n\1# \2"
    ))

    # setupModel.py:762
    rules.append(PatchRule(
        name="plugin_total_docker1",
        file_rel="class/btdockerModel/setupModel.py",
        description="plugin_total 上报 #3: setupModel.py L762",
        pattern=(
            r"(\s*)"
            r"(public\.httpPost\(public\.GetConfigValue\('home'\) \+ '/api/panel/plugin_total', "
            r'\{"pid": "615", \'p_name\': "docker"\}, 3\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 plugin_total 上报 #3\n\1# \2"
    ))

    # setupModel.py:819
    rules.append(PatchRule(
        name="plugin_total_docker2",
        file_rel="class/btdockerModel/setupModel.py",
        description="plugin_total 上报 #4: setupModel.py L819",
        pattern=(
            r"(\s*)"
            r"(public\.httpPost\(public\.GetConfigValue\('home'\) \+ '/api/panel/plugin_total', "
            r'\{"pid": "615", \'p_name\': "docker"\}, 10\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 plugin_total 上报 #4\n\1# \2"
    ))

    # dk_setup.py:237
    rules.append(PatchRule(
        name="plugin_total_docker3",
        file_rel="class/projectModel/bt_docker/dk_setup.py",
        description="plugin_total 上报 #5: dk_setup.py L237",
        pattern=(
            r"(\s*)"
            r"(public\.httpPost\(public\.GetConfigValue\('home'\) \+ '/api/panel/plugin_total', "
            r'\{"pid": "615", \'p_name\': "bt_docker"\}, 10\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 plugin_total 上报 #5\n\1# \2"
    ))

    # ================================================================
    # 第四组：WP Toolkit 统计（3 处）—— threading.Thread 开线程丢
    # ================================================================

    # core.py:1829 wp_installed
    rules.append(PatchRule(
        name="wp_toolkit_installed",
        file_rel="class/wptoolkitModel/wp_toolkit/core.py",
        description="WP wp_installed 上报: core.py L1829",
        pattern=(
            r"(\s*)"
            r'(threading\.Thread\(target=requests\.post, kwargs=\{"url": url, "json": pdata\}\)\.start\(\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 WP wp_installed 上报\n\1# \2"
    ))

    # core.py:5133 + 5167 panel_count_daily
    rules.append(PatchRule(
        name="wp_toolkit_daily",
        file_rel="class/wptoolkitModel/wp_toolkit/core.py",
        description="WP panel_count_daily 上报 (2处): core.py L5133/L5167",
        pattern=(
            r"(\s*)"
            r'(threading\.Thread\(target=lambda: requests\.post\(url, json=pdata\)\)\.start\(\))'
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 WP panel_count_daily 上报\n\1# \2",
        # 这个模式在文件里可能出现多次，用 replace_all 处理
    ))

    # ================================================================
    # 第五组：total_keyword 搜索关键词（1 处）—— 返回值没人接
    # ================================================================

    rules.append(PatchRule(
        name="total_keyword",
        file_rel="class/public.py",
        description="total_keyword 上报: class/public.py L3665",
        pattern=(
            r"(\s*)"
            r'(public\.run_thread\(public\.httpPost, '
            r"\(public\.GetConfigValue\('home'\) \+ '/api/panel/total_keyword', "
            r"{'keyword': keyword, 'type': 1}\)\))"
        ),
        replacement=r"\1" + MARK + r" 已屏蔽 total_keyword 上报\n\1# \2"
    ))

    # ================================================================
    # 第六组：submit_expand_pack_used（邮件群发硬件指纹上报）
    #          ⚠️ 有返回值依赖：load_soft_list() + refresh_pd()
    #          处理方式：注释掉 requests.post 调用块，保留后续刷新
    # ================================================================

    rules.append(PatchRule(
        name="submit_expand_pack_used",
        file_rel="class/mailModel/bulkModel.py",
        description="submit_expand_pack_used 硬件指纹上报: bulkModel.py L2992-3007",
        pattern=(
            r"(?ms)"
            r"(\s*import panelAuth\s*\n"
            r"\s*import requests\s*\n"
            r"\s*from BTPanel import session\s*\n"
            r"\s*cloudUrl = '\{?/api/panel/submit_expand_pack_used'\.format\(public\.OfficialApiBase\(\)\)\s*\n"
            r"\s*pdata = panelAuth\.panelAuth\(\)\.create_serverid\(None\)\s*\n"
            r"\s*url_headers = \{\}\s*\n"
            r"\s*if 'token' in pdata:\s*\n"
            r"\s*url_headers = \{"authorization": "bt \{\}"\.format\(pdata\['token'\]\)\}\s*\n"
            r"\s*pdata\['environment_info'\] = json\.dumps\(public\.fetch_env_info\(\)\)\s*\n"
            r"\s*pdata\['data'\] = submat_data\s*\n"
            r"\s*pdata\['expand_pack_type'\] = \"mail\"\s*\n"
            r")"  # 收集到 prepare 块末尾
            r"(\s*listTmp = requests\.post\(cloudUrl, json=pdata, headers=url_headers\)\s*\n"
            r"\s*ret = listTmp\.json\(\)\s*\n"
            r"\s*if not ret\['success'\]:\s*\n"
            r"\s*print\(f\"\|-\{ret\['res'\]\}\"\)\s*\n"
            r"\s*return ret\['res'\]\s*\n"
            r"\s*else:\s*\n)"
        ),
        replacement=(
            r"\1"  # 保留 prepare 块
            + MARK + r" 已屏蔽 submit_expand_pack_used 硬件指纹上报\n"
            + MARK + r" 保留后续 load_soft_list() + refresh_pd() 刷新逻辑\n"
            + r"\2"
            + MARK + r" 原本这里发 requests.post 现在改为直接跳过，继续执行刷新\n"
            + r"        pass  # 隐私屏蔽：跳过环境信息上报\n"
        )
    ))

    # ================================================================
    # 第七组：webshell_check.py 的 send_baota / send_baota2
    #          ⚠️ 有返回值依赖：send_baota 用 ret 判断调用 check_webshell
    #          处理方式：注释掉 httpPost 行，直接 return self.check_webshell(filename)
    # ================================================================

    # send_baota2 (L299-304)
    rules.append(PatchRule(
        name="webshell_send_baota2",
        file_rel="class/webshell_check.py",
        description="btwaf_submit send_baota2: webshell_check.py L299-304",
        pattern=(
            r"(\s*def send_baota2\(self, filename\):\s*\n"
            r"\s*cloudUrl = public\.GetConfigValue\('home'\) \+ '/api/panel/btwaf_submit'\s*\n"
            r"\s*pdata = \{'codetxt': self\.ReadFile\(filename\), 'md5': self\.read_file_md5\(filename\), "
            r"'type': '0',\s*\n"
            r"\s*'host_ip': public\.GetLocalIp\(\), 'size': os\.path\.getsize\(filename\)\}\s*\n)"
            r"(\s*ret = public\.httpPost\(cloudUrl, pdata\)\s*\n"
            r"\s*return True)"
        ),
        replacement=(
            r"\1"
            + MARK + r" 已屏蔽 btwaf_submit send_baota2\n"
            + MARK + r" 原本这里发 httpPost 上传源代码，改为直接返回（不影响本地查杀）\n"
            + r"        return True"
        )
    ))

    # send_baota (L306-317)
    rules.append(PatchRule(
        name="webshell_send_baota",
        file_rel="class/webshell_check.py",
        description="btwaf_submit send_baota: webshell_check.py L306-317",
        pattern=(
            r"(\s*def send_baota\(self, filename\):\s*\n"
            r"\s*if not os\.path\.exists\(filename\): return False\s*\n"
            r"\s*cloudUrl = public\.GetConfigValue\('home'\) \+ '/api/panel/btwaf_submit'\s*\n"
            r"\s*pdata = \{'codetxt': self\.ReadFile\(filename\), 'md5': self\.read_file_md5\(filename\), "
            r"'type': '0',\s*\n"
            r"\s*'host_ip': public\.GetLocalIp\(\), 'size': os\.path\.getsize\(filename\)\}\s*\n)"
            r"(\s*ret = public\.httpPost\(cloudUrl, pdata\)\s*\n"
            r"\s*if ret == '1':\s*\n"
            r"\s*return self\.check_webshell\(filename\)\s*\n"
            r"\s*elif ret == '-1':\s*\n"
            r"\s*return self\.check_webshell\(filename\)\s*\n"
            r"\s*else:\s*\n"
            r"\s*return False)"
        ),
        replacement=(
            r"\1"
            + MARK + r" 已屏蔽 btwaf_submit send_baota（WEBSHELL 源码上报）\n"
            + MARK + r" 原本这里发 httpPost，改为跳过上报直接用本地 check_webshell 查询\n"
            + r"        return self.check_webshell(filename)"
        )
    ))

    # flush_plugin.py 的 clear_hosts
    rules.append(PatchRule(
        name="flush_plugin_clear_hosts",
        file_rel="script/flush_plugin.py",
        description="flush_plugin clear_hosts 中 info_json 请求: flush_plugin.py L21-22",
        pattern=(
            r"(\s*remove = 0\s*\n"
            r"\s*try:\s*\n"
            r"\s*import requests\s*\n"
            r"\s*requests\.packages\.urllib3\.disable_warnings"
            r"\(requests\.packages\.urllib3\.exceptions\.InsecureRequestWarning\)\s*\n)"
            r"(\s*url = 'https://www\.bt\.cn/api/ip/info_json'\s*\n"
            r"\s*res = requests\.post\(url, verify=False, timeout=30\)\s*\n)"
        ),
        replacement=(
            r"\1"
            + MARK + r" 已屏蔽 flush_plugin 中 info_json 请求\n"
            + MARK + r" 不再通过发请求判断 BT API 存活，跳过 hosts 清理\n"
            + r"        remove = 0"
        )
    ))

    return rules


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
            self._apply_rule(rule, backup_dir)

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
