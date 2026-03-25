import os
import re
import shutil
import stat
import subprocess
import time
import shlex  # 用于安全地拆分带有空格的复杂编译器参数
from collections import Counter
from configparser import ConfigParser


# ======================================================================
# 基础工具函数 (保持不变)
# ======================================================================

def run_executable(exe_path):
    exe_abs_path = os.path.abspath(exe_path)
    try:
        if not os.path.exists(exe_abs_path):
            return {"success": False, "exit_code": -4, "output": "", "error": f"文件不存在: {exe_abs_path}"}
        result = subprocess.run([exe_abs_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', timeout=5)
        return {"success": True, "exit_code": result.returncode, "output": result.stdout.strip(), "error": ""}
    except subprocess.TimeoutExpired:
        return {"success": False, "exit_code": -1, "output": "", "error": f"运行超时: {exe_abs_path}"}
    except Exception as e:
        return {"success": False, "exit_code": -2, "output": "", "error": f"运行异常: {str(e)}"}


def fix_executable_permission(exe_path):
    exe_abs_path = os.path.abspath(exe_path)
    if os.path.exists(exe_abs_path):
        os.chmod(exe_abs_path, stat.S_IRWXU)
        return True, exe_abs_path
    return False, exe_abs_path


def run_sanitizer_check(compiler, src, output_prefix):
    exe_path = f"{output_prefix}_sanitizer.out"
    cmd = [compiler, src, "-O0", "-g", "-fsanitize=address,undefined", "-o", exe_path]
    log_info = f"Sanitizer 编译命令：{' '.join(cmd)}\n"
    try:
        compile_res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        if compile_res.returncode != 0:
            return log_info + f"编译失败（或不支持这些 Sanitizer）：\n{compile_res.stdout}\n"
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

    # 4. 优化后 IR & Pass 详情
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
        backend_cmd = [compiler, src] + flags_list + ["-g", "-c", "-o", f"{output_prefix}.o", "-v"]
        subprocess.run(backend_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs["backend"] = f"输出目标文件：{output_prefix}.o\n"
        logs["interleaved_asm"] = get_interleaved_asm(f"{output_prefix}.o")
        cfg_cmd = [compiler, src] + flags_list + ["-mllvm", "-dot-cfg"]
        subprocess.run(cfg_cmd, cwd=os.path.dirname(output_prefix), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        logs["backend"] += f"\n注：控制流图 (.dot) 已尝试输出至当前工作目录。\n"
    except Exception as e:
        logs["backend"] = f"后端编译失败：{str(e)}\n"

    return logs


# ======================================================================
# 核心拆解 1：针对单个测试用例生成完整差分日志
# ======================================================================
def generate_single_case_log(bugid, compiler, src_file_path, work_dir, rightOption, wrongOption,
                             log_filename="compile.log"):
    """
    独立处理单个 C 文件，生成它的专有差分日志。
    所有的中间文件 (.s, .o, .ll) 都会安全地存放在 work_dir 下。
    """
    if not os.path.exists(work_dir):
        os.makedirs(work_dir)

    log_file = os.path.join(work_dir, log_filename)

    compile_configs = [
        {"label": "【参照组 - 正确表现】", "flags": rightOption, "prefix": os.path.join(work_dir, "pass_cfg")},
        {"label": "【对照组 - 触发Bug】", "flags": wrongOption, "prefix": os.path.join(work_dir, "fail_cfg")}
    ]

    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"【深度编译差分分析报告 - 独立变异体】 | BugID: {bugid}\n")
        f.write(f"源文件: {src_file_path}\n")
        f.write(f"编译器: {compiler}\n")
        f.write("=" * 100 + "\n\n")

        # 0. Sanitizer 检查
        f.write("【🔬 源码安全性基准分析 (Sanitizer)】\n" + "=" * 100 + "\n")
        sanitizer_log = run_sanitizer_check(compiler, src_file_path, os.path.join(work_dir, "sanitizer_check"))
        f.write(sanitizer_log)
        f.write("=" * 100 + "\n\n")

    compile_configs = [
        {"label": "【参照组 - 正确表现】", "flags": rightOption, "prefix": os.path.join(work_dir, "pass_cfg")},
        {"label": "【对照组 - 触发Bug】", "flags": wrongOption, "prefix": os.path.join(work_dir, "fail_cfg")}
    ]

    run_results = []
    for cfg_dict in compile_configs:
        exe_path = cfg_dict["prefix"] + "_exe"
        flags_list = shlex.split(cfg_dict["flags"])
        full_cmd = [compiler, src_file_path] + flags_list + ["-march=x86-64", "-g", "-ftime-report", "-v", "-o",
                                                             exe_path]

        start_time = time.time()
        compile_result = subprocess.run(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        duration = round(time.time() - start_time, 2)

        exe_exists, exe_abs_path = fix_executable_permission(exe_path) if compile_result.returncode == 0 else (
        False, "")
        run_res = run_executable(exe_abs_path) if exe_exists else {"success": False, "exit_code": -3, "output": "N/A",
                                                                   "error": "编译失败"}
        run_results.append(run_res)

        stage_logs = get_detailed_compile_logs(compiler, src_file_path, cfg_dict["flags"], cfg_dict["prefix"])

        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"{cfg_dict['label']} 深度追踪\n")
            f.write(f"编译参数: {cfg_dict['flags']}\n" + "-" * 80 + "\n")
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
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write("【🚨 核心差异对比分析】\n" + "=" * 100 + "\n")
        try:
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

    return log_file


# ======================================================================
# 核心拆解 2：批量调度器，自动处理 10 个测试用例
# ======================================================================
def batch_generate_logs(bugid, rev, configFile, rightOption, wrongOption):
    """
    主控调度函数：遍历 5 个 passing cases 和 5 个 failing cases。
    为每一个 case 建立独立的子文件夹进行编译追踪，防止中间文件相互覆盖。
    """
    cfg = ConfigParser()
    cfg.read(configFile)
    compilerBasePath = cfg.get('llvm-locations', 'compilersdir')
    logBaseDir = cfg.get('llvm-locations', 'logDir')
    basePassDir = cfg.get('llvm-locations', 'passdir')
    baseInfoDir = cfg.get('llvm-locations', 'infodir')
    passDir = os.path.join(basePassDir, bugid, 'passing_cases')
    failDir = os.path.join(basePassDir, bugid, 'failing_cases')

    compiler = os.path.join(compilerBasePath, rev, f'{rev}-build', 'bin', 'clang')
    base_work_dir = os.path.join(logBaseDir, bugid, "mutants_logs")
    failPath = os.path.join(baseInfoDir, bugid, 'fail.c')
    workPath = os.path.join(logBaseDir, bugid)
    generate_single_case_log(bugid, compiler, failPath, workPath, rightOption, wrongOption)

    print(f"\n🚀 开始为 BugID: {bugid} 批量生成变异体的差分编译日志...")

    # 获取测试用例列表 (各限 5 个)
    passing_files = [os.path.join(passDir, f) for f in os.listdir(passDir) if f.endswith('.c')][:5] if os.path.exists(
        passDir) else []
    failing_files = [os.path.join(failDir, f) for f in os.listdir(failDir) if f.endswith('.c')][:5] if os.path.exists(
        failDir) else []

    all_cases = []
    for f in passing_files:
        all_cases.append({"path": f, "type": "pass"})
    for f in failing_files:
        all_cases.append({"path": f, "type": "fail"})

    if not all_cases:
        print("⚠️ 未找到任何变异测试用例 (.c)！请检查 passDir 和 failDir 路径。")
        return

    generated_logs = []

    # 逐一处理 10 个用例
    for idx, case in enumerate(all_cases, 1):
        src_path = case["path"]
        case_name = os.path.basename(src_path).replace('.c', '')
        # 隔离工作区命名：比如 logDir/27868/mutants_logs/pass_case1/
        case_work_dir = os.path.join(base_work_dir, f"{case['type']}_{case_name}")

        if not os.path.exists(case_work_dir):
            os.makedirs(case_work_dir)

        # 拷贝源文件到独立工作区
        target_c_path = os.path.join(case_work_dir, os.path.basename(src_path))
        shutil.copyfile(src_path, target_c_path)

        print(f"[{idx}/{len(all_cases)}] 正在深度剖析测试用例: {case['type']} - {case_name} ...")

        # 调用单文件生成核心
        log_path = generate_single_case_log(
            bugid=bugid,
            compiler=compiler,
            src_file_path=target_c_path,
            work_dir=case_work_dir,
            rightOption=rightOption,
            wrongOption=wrongOption,
            log_filename=f"{case['type']}_{case_name}_compile.log"
        )
        generated_logs.append(log_path)

    print("\n✅ 所有 10 个变异用例的差分日志已全部生成完毕！")
    print(f"📂 存放根目录: {base_work_dir}")
    return generated_logs
