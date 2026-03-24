import os
import re
import shutil
import stat
import subprocess
import time
import shlex
from collections import Counter
from configparser import ConfigParser


# ======================== 基础工具函数 ========================

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
    """拆解编译阶段并收集详尽日志 (原汁原味保留)"""
    logs = {"preprocess": "", "ast": "", "ir_raw": "", "ir_opt": "", "ir_all_passes": "", "asm": "", "opt_passes": "",
            "backend": "", "interleaved_asm": ""}
    flags_list = shlex.split(flags_str)

    # 1. 预处理
    try:
        pre_cmd = [compiler, src] + flags_list + ["-E", "-x", "c", "-o", f"{output_prefix}.i"]
        subprocess.run(pre_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        logs["preprocess"] = f"预处理成功，输出至：{output_prefix}.i\n"
    except Exception as e:
        logs["preprocess"] = f"预处理失败：{str(e)}\n"

    # 2. AST分析
    try:
        ast_cmd = [compiler, src] + flags_list + ["-Xclang", "-ast-dump", "-fsyntax-only"]
        ast_result = subprocess.run(ast_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding='utf-8')
        funcs = len(re.findall(r'FunctionDecl', ast_result.stdout))
        logs["ast"] = f"AST生成完成，函数声明数：{funcs}\n"
    except Exception as e:
        logs["ast"] = f"AST生成异常：{str(e)}\n"

    # 3. 未优化 IR
    try:
        ir_raw_cmd = [compiler, src] + flags_list + ["-S", "-emit-llvm", "-O0", "-o", f"{output_prefix}_raw.ll"]
        subprocess.run(ir_raw_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs["ir_raw"] = f"输出文件：{output_prefix}_raw.ll\n"
    except Exception as e:
        logs["ir_raw"] = f"未优化IR生成失败：{str(e)}\n"

    # 4. 优化后 IR & Pass 详情 & 全程 IR 快照
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

    # 5. 汇编代码
    try:
        asm_cmd = [compiler, src] + flags_list + ["-S", "-fverbose-asm", "-g", "-o", f"{output_prefix}.s"]
        subprocess.run(asm_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs["asm"] = f"输出文件：{output_prefix}.s\n"
    except Exception as e:
        logs["asm"] = f"汇编生成失败：{str(e)}\n"

    # 6. 后端生成
    try:
        backend_cmd = [compiler, src] + flags_list + ["-g", "-c", "-o", f"{output_prefix}.o"]
        subprocess.run(backend_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs["backend"] = f"输出目标文件：{output_prefix}.o\n"
        logs["interleaved_asm"] = get_interleaved_asm(f"{output_prefix}.o")
    except Exception as e:
        logs["backend"] = f"后端编译失败：{str(e)}\n"

    return logs


# ======================== 核心改进：动态获取 Pass 并缩减 ========================

def get_llvm_pass_arguments(bin_dir, opt_level_flag):
    """根据优化级别（如-Os, -O3）动态提取 Pass 序列 (已修复 TypeError)"""
    opt_exe = os.path.join(bin_dir, 'opt')
    clang_exe = os.path.join(bin_dir, 'clang')
    dummy_ll = "temp_dummy.ll"
    dummy_bc = "temp_dummy.bc"

    with open(dummy_ll, "w") as f:
        f.write("define i32 @main() { ret i32 0 }\n")

    try:
        subprocess.run([clang_exe, "-c", "-emit-llvm", dummy_ll, "-o", dummy_bc], stderr=subprocess.DEVNULL)
        cmd = [opt_exe, opt_level_flag, "-disable-output", "-debug-pass=Arguments", dummy_bc]

        # 修复点：stderr 被重定向到 stdout 后，需从 stdout 读取内容
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        output_text = res.stdout if res.stdout else ""
        all_args = re.findall(r'-(?:[\w-]+)', output_text)

        exclude = {'-disable-output', '-debug-pass', opt_level_flag}
        final_passes = [p for p in all_args if p not in exclude]

        for f in [dummy_ll, dummy_bc]:
            if os.path.exists(f): os.remove(f)
        return final_passes
    except Exception as e:
        print(f"动态获取 Pass 失败: {str(e)}")
        return []


def run_manual_pass_reduction(bin_dir, src_path, opt_level, good_output, work_dir):
    """通用 Delta Debugging 引擎：支持动态 Pass 序列"""
    opt_exe = os.path.abspath(os.path.join(bin_dir, 'opt'))
    llc_exe = os.path.abspath(os.path.join(bin_dir, 'llc'))
    clang_exe = os.path.abspath(os.path.join(bin_dir, 'clang'))

    log_info = f"\n【🛠️ 动态自动化 Pass 缩减定位报告 - {opt_level}】\n" + "=" * 100 + "\n"

    # 1. 动态获取初始 Pass 序列
    initial_passes = get_llvm_pass_arguments(bin_dir, opt_level)
    if not initial_passes:
        return log_info + "❌ 无法提取 Pass 序列，缩减引擎无法启动。\n"

    bc_raw = os.path.join(work_dir, "reducer_raw.bc")
    bc_opt = os.path.join(work_dir, "reducer_opt.bc")
    obj_out = os.path.join(work_dir, "reducer_out.o")
    exe_out = os.path.join(work_dir, "reducer_exe")

    subprocess.run([clang_exe, "-O0", "-emit-llvm", "-c", src_path, "-o", bc_raw], check=True)

    def check_if_bug_exists(pass_list):
        if subprocess.run([opt_exe] + pass_list + [bc_raw, "-o", bc_opt],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return False
        if subprocess.run([llc_exe, "-filetype=obj", bc_opt, "-o", obj_out],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return False
        if subprocess.run(["gcc", "-no-pie", obj_out, "-lm", "-o", exe_out],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return False
        try:
            res = subprocess.run([exe_out], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=3)
            return res.returncode != 0 or res.stdout.strip() != good_output
        except:
            return True

    print(f"  -> 🤖 动态获取到 {len(initial_passes)} 个 Pass，正在缩减...")

    if not check_if_bug_exists(initial_passes):
        return log_info + f"❌ 初始 Pass 序列 ({opt_level}) 无法复现 Bug，可能需要额外的 Clang 参数。\n"

    current_passes = initial_passes.copy()
    i = len(current_passes) - 1
    while i >= 0:
        test_list = current_passes[:i] + current_passes[i + 1:]
        if check_if_bug_exists(test_list):
            current_passes = test_list
            print(f"  [+] 成功剔除，剩余 Pass 数: {len(current_passes)}")
        i -= 1

    log_info += f"🎯 最终锁定导致问题的最小核心 Pass 序列 ({len(current_passes)} 个):\n  "
    log_info += "\n  ".join(current_passes) + "\n\n"
    log_info += f"复现命令：\n{opt_exe} {' '.join(current_passes)} {bc_raw} -o buggy.bc\n"
    return log_info


# ======================== 主控函数 ========================

def generate_logs(bugid, rev, configFile, rightOption, wrongOption):
    """主控函数：完整保留所有日志收集阶段"""
    cfg = ConfigParser()
    cfg.read(configFile)
    infoBasePath = cfg.get('llvm-locations', 'infodir')
    compilerBasePath = cfg.get('llvm-locations', 'compilersdir')
    logBaseDir = cfg.get('llvm-locations', 'logDir')

    work_dir = os.path.join(logBaseDir, bugid)
    if not os.path.exists(work_dir): os.makedirs(work_dir)

    source_file = os.path.join(infoBasePath, bugid, 'fail.c')
    fail_c_path = os.path.join(work_dir, 'fail.c')
    if not os.path.exists(fail_c_path):
        shutil.copyfile(source_file, fail_c_path)

    bin_dir = os.path.join(compilerBasePath, rev, f'{rev}-build', 'bin')
    compiler = os.path.join(bin_dir, 'clang')
    log_file = os.path.join(work_dir, "compile.log")

    print(f"🚀 开始为 BugID: {bugid} 生成差分编译日志 (触发级别: {wrongOption})...")

    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"【深度编译差分分析报告】 | BugID: {bugid}\n")
        f.write(f"编译器: {compiler}\n")
        f.write("=" * 100 + "\n\n")
        f.write(run_sanitizer_check(compiler, fail_c_path, os.path.join(work_dir, "sanitizer_check")))
        f.write("=" * 100 + "\n\n")

    compile_configs = [
        {"label": "【参照组 - 正确表现】", "flags": rightOption, "prefix": os.path.join(work_dir, "pass_cfg")},
        {"label": "【对照组 - 触发Bug】", "flags": wrongOption, "prefix": os.path.join(work_dir, "fail_cfg")}
    ]

    run_results = []
    for cfg_dict in compile_configs:
        exe_path = cfg_dict["prefix"] + "_exe"
        full_cmd = [compiler, fail_c_path] + shlex.split(cfg_dict["flags"]) + ["-march=x86-64", "-g", "-v", "-o",
                                                                               exe_path]

        print(f"  -> 分析配置: {cfg_dict['label']}")
        start_time = time.time()
        compile_result = subprocess.run(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        duration = round(time.time() - start_time, 2)

        exe_exists, exe_abs_path = fix_executable_permission(exe_path) if compile_result.returncode == 0 else (
        False, "")
        run_res = run_executable(exe_abs_path) if exe_exists else {"success": False, "exit_code": -3, "output": "N/A",
                                                                   "error": "编译失败"}
        run_results.append(run_res)

        stage_logs = get_detailed_compile_logs(compiler, fail_c_path, cfg_dict["flags"], cfg_dict["prefix"])

        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"{cfg_dict['label']} 深度追踪\n")
            f.write(
                f"状态: {'✅成功' if compile_result.returncode == 0 else '❌失败'} | 退出码: {run_res['exit_code']} | 耗时: {duration}s\n")
            f.write(f"输出: {run_res['output']}\n\n")
            f.write(f"[预处理与 AST]\n{stage_logs['ast']}")
            f.write(f"[LLVM IR 快照]\n{stage_logs['ir_all_passes']}")
            f.write(f"[汇编与后端]\n{stage_logs['interleaved_asm']}\n")
            f.write("=" * 100 + "\n\n")

    res_right, res_wrong = run_results[0], run_results[1]
    if res_right["exit_code"] == 0 and (res_wrong["exit_code"] != 0 or res_right["output"] != res_wrong["output"]):
        reduction_log = run_manual_pass_reduction(
            bin_dir=bin_dir,
            src_path=fail_c_path,
            opt_level=wrongOption,
            good_output=res_right["output"],
            work_dir=work_dir
        )
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(reduction_log)
    else:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write("\n【ℹ️ 分析提示】未检测到两组运行表现存在差异，无需执行 Pass 缩减。\n")

    print(f"✅ 任务完成！结果已存至：{log_file}")