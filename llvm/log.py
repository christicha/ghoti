import os
import re
import shutil
import stat
import subprocess
import time
import shlex  # 【新增】用于安全地拆分带有空格的复杂编译器参数
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
    """获取源码与汇编交织的代码 (需系统安装 objdump)"""
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
        return f"交织反汇编生成失败（请检查系统是否安装 objdump）：{str(e)}\n"


def get_detailed_compile_logs(compiler, src, flags_str, output_prefix):
    """拆解编译阶段并收集详尽日志 (支持复杂 flags)"""
    logs = {
        "preprocess": "", "ast": "", "ir_raw": "", "ir_opt": "",
        "ir_all_passes": "", "asm": "", "opt_passes": "", "backend": "", "interleaved_asm": ""
    }

    # 【核心修改】将 "-O3 -mllvm -xxx" 安全拆分成列表 ['-O3', '-mllvm', '-xxx']
    flags_list = shlex.split(flags_str)

    # 1. 预处理
    try:
        pre_cmd = [compiler, src] + flags_list + ["-E", "-x", "c", "-o", f"{output_prefix}.i"]
        pre_result = subprocess.run(pre_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding='utf-8')
        with open(f"{output_prefix}.i", 'w', encoding='utf-8') as f:
            f.write(pre_result.stdout)
        line_count = len(pre_result.stdout.splitlines()) if pre_result.stdout else 0
        logs[
            "preprocess"] = f"预处理命令：{' '.join(pre_cmd)}\n输出文件：{output_prefix}.i\n预处理后代码行数：{line_count}\n"
    except Exception as e:
        logs["preprocess"] = f"预处理失败：{str(e)}\n"

    # 2. AST分析
    try:
        ast_cmd = [compiler, src] + flags_list + ["-Xclang", "-ast-dump", "-fsyntax-only"]
        ast_result = subprocess.run(ast_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding='utf-8')
        funcs = len(re.findall(r'FunctionDecl', ast_result.stdout))
        vars = len(re.findall(r'VarDecl', ast_result.stdout))
        exprs = len(re.findall(r'Expr', ast_result.stdout))
        logs["ast"] = f"AST命令：{' '.join(ast_cmd)}\n函数声明数：{funcs} | 变量声明数：{vars} | 表达式数：{exprs}\n"
        logs["ast"] += f"AST预览：\n{chr(10).join(ast_result.stdout.splitlines()[:5])}...\n"
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
        ir_opt_cmd = [compiler, src] + flags_list + ["-S", "-emit-llvm", "-o", f"{output_prefix}_opt.ll",
                                                     "-debug-pass=Structure"]
        opt_result = subprocess.run(ir_opt_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        pass_lines = [line.strip() for line in opt_result.stdout.splitlines() if "Running pass" in line]
        logs["opt_passes"] = f"输出文件：{output_prefix}_opt.ll\nPass 数量：{len(pass_lines)}\n" + "\n".join(pass_lines)
        logs["ir_opt"] = f"优化 IR 生成成功\n"

        ir_trace_log = f"{output_prefix}_ir_trace.log"
        ir_trace_cmd = [compiler, src] + flags_list + ["-S", "-emit-llvm", "-mllvm", "-print-after-all", "-o",
                                                       "/dev/null"]
        with open(ir_trace_log, 'w', encoding='utf-8') as f:
            subprocess.run(ir_trace_cmd, stdout=f, stderr=f, text=True)
        logs["ir_all_passes"] = f"🔥 Pass 级 IR 演进快照已保存至：{ir_trace_log}\n"

    except Exception as e:
        logs["ir_opt"] = f"优化IR生成失败：{str(e)}\n"

    # 5. 汇编代码
    try:
        asm_cmd = [compiler, src] + flags_list + ["-S", "-fverbose-asm", "-g", "-o", f"{output_prefix}.s"]
        subprocess.run(asm_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        with open(f"{output_prefix}.s", 'r', encoding='utf-8') as f:
            asm_lines = [line for line in f.readlines() if not line.startswith(('#', '.')) and line.strip()]
        logs["asm"] = f"输出文件：{output_prefix}.s\n有效指令数：{len(asm_lines)}\n"
    except Exception as e:
        logs["asm"] = f"汇编生成失败：{str(e)}\n"

    # 6. 后端生成与交织反汇编
    try:
        backend_cmd = [compiler, src] + flags_list + ["-g", "-c", "-o", f"{output_prefix}.o", "-v"]
        backend_result = subprocess.run(backend_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs["backend"] = f"输出目标文件：{output_prefix}.o\n"

        logs["interleaved_asm"] = get_interleaved_asm(f"{output_prefix}.o")

        cfg_cmd = [compiler, src] + flags_list + ["-mllvm", "-dot-cfg"]
        subprocess.run(cfg_cmd, cwd=os.path.dirname(output_prefix), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        logs["backend"] += f"\n注：控制流图 (.dot) 已尝试输出至当前工作目录。\n"

    except Exception as e:
        logs["backend"] = f"后端编译失败：{str(e)}\n"

    return logs


def generate_logs(bugid, rev, configFile, rightOption, wrongOption):
    """主控函数：仅负责调度编译并生成增强日志"""
    cfg = ConfigParser()
    cfg.read(configFile)
    infoBasePath = cfg.get('llvm-locations', 'infodir')
    compilerBasePath = cfg.get('llvm-locations', 'compilersdir')
    logBaseDir = cfg.get('llvm-locations', 'logDir')

    # 环境准备
    if not os.path.exists(logBaseDir + bugid):
        os.makedirs(logBaseDir + bugid)

    source_file = os.path.join(infoBasePath, bugid, 'fail.c')
    fail_c_path = os.path.join(logBaseDir, bugid, 'fail.c')
    if not os.path.exists(fail_c_path):
        shutil.copyfile(source_file, fail_c_path)

    compiler = os.path.join(compilerBasePath, rev, f'{rev}-build', 'bin', 'clang')
    log_file = os.path.join(logBaseDir, bugid, "compile.log")

    # 【核心修改】解耦：使用 pass_cfg 和 fail_cfg 作为固定的安全文件名前缀
    compile_configs = [
        {"label": "【参照组 - 正确表现】", "flags": rightOption, "prefix": os.path.join(logBaseDir, bugid, "pass_cfg")},
        {"label": "【对照组 - 触发Bug】", "flags": wrongOption, "prefix": os.path.join(logBaseDir, bugid, "fail_cfg")}
    ]

    print(f"🚀 开始为 BugID: {bugid} 生成差分编译日志...")

    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"【深度编译差分分析报告】 | BugID: {bugid}\n")
        f.write(f"源文件: {fail_c_path}\n")
        f.write(f"编译器: {compiler}\n")
        f.write(f"参照参数 (Right): '{rightOption}'\n")
        f.write(f"触发参数 (Wrong): '{wrongOption}'\n")
        f.write("=" * 100 + "\n\n")

        # 0. 提前执行 Sanitizer 检查
        f.write("【🔬 源码安全性基准分析 (Sanitizer)】\n")
        f.write("=" * 100 + "\n")
        sanitizer_log = run_sanitizer_check(compiler, fail_c_path, os.path.join(logBaseDir, bugid, "sanitizer_check"))
        f.write(sanitizer_log)
        f.write("=" * 100 + "\n\n")

    # 分别执行两组配置
    for cfg_dict in compile_configs:
        exe_path = cfg_dict["prefix"] + "_exe"
        flags_list = shlex.split(cfg_dict["flags"])

        # 将拆分后的参数拼接到命令中
        full_cmd = [compiler, fail_c_path] + flags_list + ["-march=x86-64", "-g", "-ftime-report", "-v", "-o", exe_path]

        print(f"  -> 分析配置: {cfg_dict['label']} -> {cfg_dict['flags']}")
        start_time = time.time()
        compile_result = subprocess.run(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        duration = round(time.time() - start_time, 2)
        compile_success = compile_result.returncode == 0

        exe_exists, exe_abs_path = fix_executable_permission(exe_path) if compile_success else (False, "")
        run_result = run_executable(exe_abs_path) if exe_exists else {"success": False, "exit_code": -3,
                                                                      "output": "N/A", "error": "编译失败"}

        # 传入完整的参数字符串供底层解析
        stage_logs = get_detailed_compile_logs(compiler, fail_c_path, cfg_dict["flags"], cfg_dict["prefix"])

        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"{cfg_dict['label']} 深度追踪\n")
            f.write(f"编译参数: {cfg_dict['flags']}\n")
            f.write("-" * 80 + "\n")
            f.write(
                f"[1. 运行结果]\n状态: {'✅成功' if compile_success else '❌失败'} | 退出码: {run_result['exit_code']} | 耗时: {duration}s\n")
            f.write(f"标准输出: {run_result['output']}\n\n")

            f.write(f"[2. 预处理与 AST]\n{stage_logs['preprocess']}{stage_logs['ast']}\n")
            f.write(
                f"[3. LLVM IR 与优化阶段]\n{stage_logs['ir_raw']}{stage_logs['ir_opt']}{stage_logs['ir_all_passes']}")
            f.write(f"Pass 执行流:\n  {stage_logs['opt_passes'].replace(chr(10), chr(10) + '  ')}\n\n")
            f.write(f"[4. 汇编与后端]\n{stage_logs['asm']}{stage_logs['backend']}\n")
            f.write(f"[5. 源码交织反汇编 (重点对齐区)]\n{stage_logs['interleaved_asm']}\n")
            f.write("=" * 100 + "\n\n")

    # 对比差异生成
    print("  -> 正在生成指令与 Pass 对比分析...")
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write("【🚨 核心差异对比分析】\n")
        f.write("=" * 100 + "\n")
        try:
            # 读取统一前缀的文件
            pass_s_path = f"{compile_configs[0]['prefix']}.s"
            fail_s_path = f"{compile_configs[1]['prefix']}.s"

            if os.path.exists(pass_s_path) and os.path.exists(fail_s_path):
                with open(pass_s_path, 'r') as f2, open(fail_s_path, 'r') as f3:
                    pass_asm = [line.strip().split()[0] for line in f2 if
                                not line.startswith(('#', '.')) and line.strip()]
                    fail_asm = [line.strip().split()[0] for line in f3 if
                                not line.startswith(('#', '.')) and line.strip()]

                pass_cnt, fail_cnt = Counter(pass_asm), Counter(fail_asm)
                diff_inst = [f"{inst}: 参照组={pass_cnt.get(inst, 0)} | 触发组={fail_cnt.get(inst, 0)}"
                             for inst in set(pass_cnt) | set(fail_cnt) if pass_cnt.get(inst) != fail_cnt.get(inst)]

                f.write(f"[汇编指令集突变分析]\n")
                f.write(f"指令总量差异: 参照组({len(pass_asm)}条) vs 触发组({len(fail_asm)}条)\n")
                f.write("增删改指令列表:\n  " + "\n  ".join(diff_inst) + "\n\n")
        except Exception as e:
            f.write(f"差异对比生成失败: {str(e)}\n")

    print(f"✅ 任务完成！差分分析日志已输出至：{log_file}")

# ====== 测试入口示例 ======
# if __name__ == "__main__":
#     # 测试复杂参数是否能正确解析执行
#     generate_logs("15920", "r181189", "config.ini", "-O1 -fno-strict-aliasing", "-O3 -mllvm -disable-llvm-optzns")