import os
import re
import shutil
import stat
import subprocess
import time
import shlex
from collections import Counter
from configparser import ConfigParser


def run_executable(exe_path):
    """运行可执行文件（使用绝对路径，并捕获所有输出）"""
    exe_abs_path = os.path.abspath(exe_path)
    try:
        if not os.path.exists(exe_abs_path):
            return {"success": False, "exit_code": -4, "output": "", "error": f"文件不存在: {exe_abs_path}"}

        result = subprocess.run(
            [exe_abs_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            timeout=5
        )
        return {
            "success": True,
            "exit_code": result.returncode,
            "output": result.stdout.strip(),
            "error": ""
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "exit_code": -1, "output": "", "error": f"运行超时: {exe_abs_path}"}
    except Exception as e:
        return {"success": False, "exit_code": -2, "output": "", "error": f"运行异常: {str(e)}"}


def fix_executable_permission(exe_path):
    """修复可执行文件权限"""
    exe_abs_path = os.path.abspath(exe_path)
    if os.path.exists(exe_abs_path):
        os.chmod(exe_abs_path, stat.S_IRWXU)
        return True, exe_abs_path
    return False, exe_abs_path


def run_sanitizer_check(compiler, src, output_prefix):
    """运行未定义行为 (UB) 和内存越界检查"""
    exe_path = f"{output_prefix}_sanitizer.out"
    cmd = [compiler, src, "-O0", "-g", "-fsanitize=address,undefined", "-o", exe_path]
    log_info = f"Sanitizer 编译命令：{' '.join(cmd)}\n"

    try:
        compile_res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        if compile_res.returncode != 0:
            return log_info + f"编译失败（或当前编译器版本不支持这些 Sanitizer）：\n{compile_res.stdout}\n"

        fix_executable_permission(exe_path)
        run_res = run_executable(exe_path)

        if run_res['exit_code'] != 0 or "runtime error" in run_res['output'].lower():
            log_info += f"⚠️ 警告：检测到潜在的未定义行为 (UB)！\n运行输出：\n{run_res['output']}\n"
        else:
            log_info += f"✅ Sanitizer 检查通过，未发现明显的 UB。\n"
    except Exception as e:
        log_info += f"Sanitizer 检查异常：{str(e)}\n"

    return log_info


def get_interleaved_asm(obj_file):
    """获取源码与汇编交织的代码"""
    if not os.path.exists(obj_file):
        return "目标文件不存在，无法生成交织反汇编。\n"
    try:
        cmd = ["objdump", "-d", "-S", "-l", obj_file]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        dump_file = obj_file + ".dump"
        with open(dump_file, 'w', encoding='utf-8') as f:
            f.write(res.stdout)

        preview = "\n".join(res.stdout.splitlines()[:50])
        return f"交织反汇编已生成至：{dump_file}\n预览（前 50 行）：\n{preview}\n...\n"
    except Exception as e:
        return f"交织反汇编生成失败：{str(e)}\n"


def get_detailed_compile_logs(compiler, src, flags_str, output_prefix):
    """拆解编译阶段并收集详尽日志"""
    logs = {"preprocess": "", "ast": "", "ir_raw": "", "ir_opt": "", "ir_all_passes": "", "asm": "", "opt_passes": "",
            "backend": "", "interleaved_asm": ""}
    flags_list = shlex.split(flags_str)

    try:
        pre_cmd = [compiler, src] + flags_list + ["-E", "-x", "c", "-o", f"{output_prefix}.i"]
        subprocess.run(pre_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        logs["preprocess"] = f"预处理成功，输出至：{output_prefix}.i\n"
    except Exception as e:
        logs["preprocess"] = f"预处理失败：{str(e)}\n"

    try:
        ast_cmd = [compiler, src] + flags_list + ["-Xclang", "-ast-dump", "-fsyntax-only"]
        ast_result = subprocess.run(ast_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding='utf-8')
        funcs = len(re.findall(r'FunctionDecl', ast_result.stdout))
        logs["ast"] = f"AST生成完成，函数声明数：{funcs}\n"
    except Exception as e:
        logs["ast"] = f"AST生成异常：{str(e)}\n"

    try:
        ir_raw_cmd = [compiler, src] + flags_list + ["-S", "-emit-llvm", "-O0", "-o", f"{output_prefix}_raw.ll"]
        subprocess.run(ir_raw_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs["ir_raw"] = f"输出文件：{output_prefix}_raw.ll\n"
    except Exception as e:
        logs["ir_raw"] = f"未优化IR生成失败：{str(e)}\n"

    try:
        ir_opt_cmd = [compiler, src] + flags_list + ["-S", "-emit-llvm", "-o", f"{output_prefix}_opt.ll", "-mllvm",
                                                     "-debug-pass=Arguments"]
        opt_result = subprocess.run(ir_opt_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        pass_lines = [line.strip() for line in opt_result.stdout.splitlines() if
                      "Pass" in line or "pass" in line.lower()]
        logs["opt_passes"] = f"输出文件：{output_prefix}_opt.ll\nPass 结构输出行数：{len(pass_lines)}\n" + "\n".join(
            pass_lines)
        logs["ir_opt"] = f"优化 IR 生成成功\n"

        ir_trace_log = f"{output_prefix}_ir_trace.log"
        ir_trace_cmd = [compiler, src] + flags_list + ["-S", "-emit-llvm", "-mllvm", "-print-after-all", "-o",
                                                       "/dev/null"]

        with open(ir_trace_log, 'w', encoding='utf-8') as f:
            trace_res = subprocess.run(ir_trace_cmd, stdout=f, stderr=subprocess.STDOUT, text=True)
            if trace_res.returncode != 0:
                f.write(
                    f"\n\n[⚠️ 警告] 编译器在生成 IR 快照时崩溃 (Exit code: {trace_res.returncode})，这是因为旧版 LLVM 打印模块存在Bug。快照截断。\n")
        logs["ir_all_passes"] = f"🔥 Pass 级 IR 演进快照已保存至：{ir_trace_log}\n"
    except Exception as e:
        logs["ir_opt"] = f"优化IR生成失败：{str(e)}\n"

    try:
        asm_cmd = [compiler, src] + flags_list + ["-S", "-fverbose-asm", "-g", "-o", f"{output_prefix}.s"]
        subprocess.run(asm_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs["asm"] = f"输出文件：{output_prefix}.s\n"
    except Exception as e:
        logs["asm"] = f"汇编生成失败：{str(e)}\n"

    try:
        backend_cmd = [compiler, src] + flags_list + ["-g", "-c", "-o", f"{output_prefix}.o"]
        subprocess.run(backend_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs["backend"] = f"输出目标文件：{output_prefix}.o\n"
        logs["interleaved_asm"] = get_interleaved_asm(f"{output_prefix}.o")
    except Exception as e:
        logs["backend"] = f"后端编译失败：{str(e)}\n"

    return logs


# ======================== 新增极速核心：手动 Delta Debugging 引擎 ========================
def run_manual_pass_reduction(bin_dir, src_path, good_output, work_dir):
    """
    自己实现一个鲁棒的增量删减工具（完全抛弃会崩溃的 bugpoint）
    """
    opt_exe = os.path.abspath(os.path.join(bin_dir, 'opt'))
    llc_exe = os.path.abspath(os.path.join(bin_dir, 'llc'))
    clang_exe = os.path.abspath(os.path.join(bin_dir, 'clang'))

    log_info = "\n【🛠️ 自定义自动化 Pass 贪心缩减定位报告】\n" + "=" * 100 + "\n"

    # 根据之前 -O3 的报错日志，提取的典型 LLVM 3.x -O3 pass 序列。
    # 🚨 已主动移除让 opt 崩溃的 `-simplify-libcalls`
    initial_passes = [
        "-tbaa", "-basicaa", "-simplifycfg", "-sroa", "-early-cse", "-lower-expect",
        "-tbaa", "-basicaa", "-globalopt", "-ipsccp", "-deadargelim", "-instcombine",
        "-simplifycfg", "-prune-eh", "-inline", "-functionattrs", "-sroa", "-early-cse",
        "-jump-threading", "-correlated-propagation", "-simplifycfg", "-instcombine",
        "-tailcallelim", "-simplifycfg", "-reassociate", "-loop-rotate", "-licm",
        "-loop-unswitch", "-instcombine", "-indvars", "-loop-idiom", "-loop-deletion",
        "-loop-unroll", "-gvn", "-memcpyopt", "-sccp", "-instcombine", "-jump-threading",
        "-correlated-propagation", "-dse", "-adce", "-simplifycfg", "-instcombine",
        "-strip-dead-prototypes", "-globaldce", "-constmerge"
    ]

    bc_raw = os.path.join(work_dir, "reducer_raw.bc")
    bc_opt = os.path.join(work_dir, "reducer_opt.bc")
    obj_out = os.path.join(work_dir, "reducer_out.o")
    exe_out = os.path.join(work_dir, "reducer_exe")

    # 1. 使用 Clang 吐出最基础的无优化 IR
    try:
        subprocess.run([clang_exe, "-O0", "-emit-llvm", "-c", src_path, "-o", bc_raw], check=True)
    except Exception as e:
        return log_info + f"❌ 生成基础 Bitcode 失败，无法进行缩减: {str(e)}\n"

    # 核心判断逻辑函数
    def check_if_bug_exists(pass_list):
        # (1) 运行 opt 进行特定序列的优化
        if subprocess.run([opt_exe] + pass_list + [bc_raw, "-o", bc_opt], stderr=subprocess.DEVNULL).returncode != 0:
            return False  # Pass组合不兼容，跳过

        # (2) 运行 llc 将 IR 编译成目标文件
        if subprocess.run([llc_exe, "-filetype=obj", bc_opt, "-o", obj_out], stderr=subprocess.DEVNULL).returncode != 0:
            return False

        # (3) 运行 gcc 链接 (必须加 -no-pie 兼容旧版 llc 输出)
        if subprocess.run(["gcc", "-no-pie", obj_out, "-lm", "-o", exe_out], stderr=subprocess.DEVNULL).returncode != 0:
            return False

        # (4) 运行并验证最终输出
        try:
            res = subprocess.run([exe_out], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=3)
            # 只要崩溃了，或者输出不是我们预期的正确值，说明 Bug 仍在！
            if res.returncode != 0 or res.stdout.strip() != good_output:
                return True
        except subprocess.TimeoutExpired:
            return True  # 死循环视同为 Bug 复现

        return False  # 跑出了正确的参照结果，说明由于少了某个 Pass，Bug 消失了

    print(f"  -> 🤖 [手动缩减引擎] 初始化完成，共捕获 {len(initial_passes)} 个 Pass。")
    print(f"  -> 正在验证初始序列...")

    if not check_if_bug_exists(initial_passes):
        log_info += "❌ 初始的 Pass 序列无法复现 Bug（程序输出了正确答案）。\n可能是该 Bug 高度依赖某个隐藏配置，缩减中止。\n"
        return log_info

    log_info += "✅ 初始序列复现 Bug 成功，开始贪心反向剔除无辜 Pass...\n"
    print(f"  -> 初始序列验证成功！开始反向贪心剔除...")

    current_passes = initial_passes.copy()
    removed_count = 0
    i = len(current_passes) - 1

    # Delta Debugging: 反向贪心遍历
    while i >= 0:
        # 尝试拿掉第 i 个 Pass
        test_list = current_passes[:i] + current_passes[i + 1:]

        # 如果拿掉后 Bug 依然存在，说明这个 Pass 是没有用的，彻底抛弃它
        if check_if_bug_exists(test_list):
            current_passes = test_list
            removed_count += 1

        i -= 1

    log_info += f"\n🎯【寻凶成功】通过 Delta Debugging 引擎，共剔除了 {removed_count} 个无关 Pass！\n"
    log_info += f"最终锁定导致错译的最小核心 Pass 序列 ({len(current_passes)} 个):\n  "
    log_info += "\n  ".join(current_passes) + "\n\n"
    log_info += "你可以使用以下命令手动复现：\n"
    log_info += f"{opt_exe} {' '.join(current_passes)} {bc_raw} -o trigger.bc\n"

    return log_info


def generate_logs(bugid, rev, configFile, rightOption, wrongOption):
    """主控函数"""
    cfg = ConfigParser()
    cfg.read(configFile)
    infoBasePath = cfg.get('llvm-locations', 'infodir')
    compilerBasePath = cfg.get('llvm-locations', 'compilersdir')
    logBaseDir = cfg.get('llvm-locations', 'logDir')

    if not os.path.exists(logBaseDir + bugid):
        os.makedirs(logBaseDir + bugid)

    source_file = os.path.join(infoBasePath, bugid, 'fail.c')
    fail_c_path = os.path.join(logBaseDir, bugid, 'fail.c')
    if not os.path.exists(fail_c_path):
        shutil.copyfile(source_file, fail_c_path)

    bin_dir = os.path.join(compilerBasePath, rev, f'{rev}-build', 'bin')
    compiler = os.path.join(bin_dir, 'clang')
    log_file = os.path.join(logBaseDir, bugid, "compile.log")

    compile_configs = [
        {"label": "【参照组 - 正确表现】", "flags": rightOption, "prefix": os.path.join(logBaseDir, bugid, "pass_cfg")},
        {"label": "【对照组 - 触发Bug】", "flags": wrongOption, "prefix": os.path.join(logBaseDir, bugid, "fail_cfg")}
    ]

    print(f"🚀 开始为 BugID: {bugid} 生成差分编译日志...")

    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"【深度编译差分分析报告】 | BugID: {bugid}\n")
        f.write(f"编译器: {compiler}\n")
        f.write("=" * 100 + "\n\n")
        # 0. Sanitizer
        f.write(run_sanitizer_check(compiler, fail_c_path, os.path.join(logBaseDir, bugid, "sanitizer_check")))
        f.write("=" * 100 + "\n\n")

    run_results = []

    # 1. 编译和生成日志
    for cfg_dict in compile_configs:
        exe_path = cfg_dict["prefix"] + "_exe"
        flags_list = shlex.split(cfg_dict["flags"])
        full_cmd = [compiler, fail_c_path] + flags_list + ["-march=x86-64", "-g", "-v", "-o", exe_path]

        print(f"  -> 分析配置: {cfg_dict['label']} -> {cfg_dict['flags']}")
        start_time = time.time()
        compile_result = subprocess.run(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        duration = round(time.time() - start_time, 2)
        compile_success = compile_result.returncode == 0

        exe_exists, exe_abs_path = fix_executable_permission(exe_path) if compile_success else (False, "")
        run_res = run_executable(exe_abs_path) if exe_exists else {"success": False, "exit_code": -3, "output": "N/A",
                                                                   "error": "编译失败"}

        run_results.append(run_res)

        stage_logs = get_detailed_compile_logs(compiler, fail_c_path, cfg_dict["flags"], cfg_dict["prefix"])

        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"{cfg_dict['label']} 深度追踪\n")
            f.write(
                f"状态: {'✅成功' if compile_success else '❌失败'} | 退出码: {run_res['exit_code']} | 耗时: {duration}s\n")
            f.write(f"输出: {run_res['output']}\n\n")
            f.write(f"[预处理与 AST]\n{stage_logs['ast']}")
            f.write(f"[LLVM IR 快照]\n{stage_logs['ir_all_passes']}")
            f.write(f"[汇编与后端]\n{stage_logs['interleaved_asm']}\n")
            f.write("=" * 100 + "\n\n")

    # 2. 手动引擎提取 Pass
    print("  -> 分析运行结果状态...")
    res_right, res_wrong = run_results[0], run_results[1]

    # 检测逻辑：如果参照组正常，且与触发组存在行为差异，则触发自定义 Pass 缩小引擎
    if res_right["exit_code"] == 0 and (res_wrong["exit_code"] != 0 or res_right["output"] != res_wrong["output"]):
        bp_log = run_manual_pass_reduction(
            bin_dir=bin_dir,
            src_path=fail_c_path,
            good_output=res_right["output"],
            work_dir=os.path.join(logBaseDir, bugid)
        )
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(bp_log)
    else:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write("\n【ℹ️ 分析提示】未检测到两组运行表现存在差异，无需执行 Pass 缩减。\n")

    print(f"✅ 任务完成！极致缩减的 Pass 寻凶结果已输出至：{log_file}")