import os
import random
import re
import time
import shutil
from configparser import ConfigParser
from openai import OpenAI, APIError, APIConnectionError, RateLimitError

from llvm.check import checkIsPass_wrongcodeOneline, checkIsPass_zeroandsegmentoneline, checkIsPass_multilineswrongcode, \
    checkIsPass_zeroandonenumber
from llvm.collectCov import collectcov

ub_set = set()
syn_err_set = set()


def remove_unused_printf(file1, file2):
    with open(file1, 'r') as f1:
        lines1 = f1.read().splitlines()

    printf_pattern = re.compile(r'\bprintf\b')
    printf_lines = set()

    for i, line in enumerate(lines1):
        if printf_pattern.search(line):
            printf_lines.add(line.strip())

    with open(file2, 'r') as f2:
        lines2 = f2.read().splitlines()

    new_lines = []
    for line in lines2:
        if not printf_pattern.search(line) or line.strip() in printf_lines:
            new_lines.append(line)

    with open(file2, 'w') as f3:
        f3.write('\n'.join(new_lines))


def insert_printf_for_wrong_code_multi(fileA, fileB):
    with open(fileA, 'r') as f:
        a_lines = f.readlines()
    with open(fileB, 'r') as f:
        b_lines = f.readlines()

    printf_lines = []
    for i, line in enumerate(a_lines):
        if 'printf' in line:
            printf_lines.append(i)

    if len(printf_lines) == 0:
        return

    for line_num in printf_lines:
        found = False
        for b_line in b_lines:
            if 'printf' in b_line and a_lines[line_num].strip() in b_line:
                found = True
                break
        if found:
            continue

        loc_indicator = a_lines[line_num + 1].strip()
        b_line_num = -1
        for i, line in enumerate(b_lines):
            if loc_indicator in line:
                b_line_num = i
                break

        if b_line_num == -1:
            continue

        b_lines.insert(b_line_num, a_lines[line_num])

    with open(fileB, 'w') as f:
        f.writelines(b_lines)


def check_oracles(file_a, file_b):
    with open(file_a, "r") as f_a:
        count_a = sum(1 for line in f_a if "printf" in line.split("//")[0] or "abort" in line.split("//")[0])
    with open(file_b, "r") as f_b:
        count_b = sum(1 for line in f_b if "printf" in line.split("//")[0] or "abort" in line.split("//")[0])
    print("\n")
    print("+++ count_a : ", count_a)
    print("+++ count_b : ", count_b)
    return count_a == count_b


def add_comment_before_string(file_path):
    pattern = "###@@@###"
    with open(file_path, 'r') as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        if pattern in line:
            idx = line.index(pattern)
            lines[i] = line[:idx] + "//" + line[idx:]

    with open(file_path, 'w') as f:
        f.writelines(lines)


def generateMutate(bugid, revision, checkpass, compileOptionRight, compileOptionWrong, configPath):
    print(
        f"\033[94m bugid:{bugid} revision:{revision} checkpass:{checkpass} compilerOptionRight:{compileOptionRight} compilerOptionWrong:{compileOptionWrong} \033[0m")
    config = ConfigParser()
    config.read(configPath)
    baseInfoDir = config.get('llvm-locations', 'infodir')
    actionPath = config.get('llvm-locations', 'actionFile')
    passdir = config.get('llvm-locations', 'passdir')
    compilerBasePath = config.get('llvm-locations', 'compilersdir')
    infoBasePath = config.get('llvm-locations', 'infodir')
    key = config.get('deepseek','api')
    url = config.get('deepseek','url')
    workpath = passdir + bugid
    if not os.path.exists(workpath):
        os.system('mkdir -p ' + workpath)
    os.chdir(workpath)
    failPath = baseInfoDir + bugid + '/fail.c'
    os.system('cp ' + failPath + ' ./main.c')
    # 创建目录结构
    os.makedirs('passing_cases', exist_ok=True)
    os.makedirs('failing_cases', exist_ok=True)
    os.makedirs('error_cases', exist_ok=True)
    os.makedirs('passcov', exist_ok=True)
    failcovPath = infoBasePath + bugid + '/'
    if os.path.exists('oriwrongfile'):
        os.system('rm oriwrongfile')
    shutil.copy2(failcovPath + 'oriwrongfile', './oriwrongfile')
    failset = set()
    failcovfile = open(failcovPath + 'failcov/stmt_info.txt')
    faillines = failcovfile.readlines()
    failcovfile.close()
    for i in range(len(faillines)):
        faillinesplit = faillines[i].strip().split(',')
        filename = re.sub(r'/CMakeFiles/[^/]+.dir', '', faillinesplit[0]).split('.gcda')[0]
        stmtlist = faillines[i].strip().split(':')[1].split(',')
        for j in range(len(stmtlist)):
            failset.add(f"{filename}:{stmtlist[j]}")

    passingcnt = 0  # passingcnt is the number of generated witness test program.
    firstfailcnt = 0
    # existingcovset is used for record the pair(passing test program, the intersection of the statement coverage of
    # this program and that of failing test program)
    existingcovset = dict()
    # unionCovwithFail is used for record the pair(passing test program, the union of the statement coverage of this
    # program and that of failing test program)
    unionCovwithFail = dict()
    # passCov is used for record the coverage of each passing test program. The form of each element is (testname,
    # pair(filename, statement))
    passCov = dict()

    failExistingCovSet = {}  # 存储当前失败用例与已有失败用例的覆盖率交集
    failUnionCovSet = {}  # 存储当前失败用例与已有失败用例的覆盖率并集
    failCov = {}  # 存储每个失败用例的完整覆盖率集合

    def diffFailCov(testname, bugid):

        failcov_dir = passdir + bugid + '/failcov/'  # faildir需和原passdir保持同层级全局变量
        if len(os.listdir(failcov_dir)) == 1:
            thisfile = open(failcov_dir + testname + '/stmt_info.txt')
            thislines = thisfile.readlines()
            thisfile.close()
            thisset = set()
            for i in range(len(thislines)):
                filenameitem = thislines[i].strip().split(':')[0]
                lineitems = thislines[i].strip().split(':')[1].split(',')
                for j in range(len(lineitems)):
                    thisset.add(filenameitem + ':' + lineitems[j])

            failExistingCovSet[testname] = set()  # 无已有用例，交集为空
            failUnionCovSet[testname] = thisset  # 并集=当前用例覆盖率
            failCov[testname] = thisset  # 保存当前用例完整覆盖率
            return 0  # different - 保留
        total_existing_fail_set = set()
        for exist_testname in failCov:
            if exist_testname == testname:
                continue
            total_existing_fail_set.update(failCov[exist_testname])
        thisfile = open(failcov_dir + testname + '/stmt_info.txt')
        thislines = thisfile.readlines()
        thisfile.close()

        thisset = set()
        for i in range(len(thislines)):
            filenameitem = thislines[i].strip().split(':')[0]
            lineitems = thislines[i].strip().split(':')[1].split(',')
            for j in range(len(lineitems)):
                thisset.add(filenameitem + ':' + lineitems[j])
        new_coverage = thisset - total_existing_fail_set
        if len(new_coverage) > 0:
            # 有新增覆盖率，保留
            failExistingCovSet[testname] = thisset & total_existing_fail_set  # 交集
            failUnionCovSet[testname] = thisset | total_existing_fail_set  # 并集
            failCov[testname] = thisset  # 保存当前覆盖率
            return 0  # 有新增，保留
        else:
            # 无新增覆盖率，舍弃
            return 1  # 无差异，舍弃

    def diffPassCov(testname, bugid):
        if len(os.listdir(passdir + bugid + '/passcov/')) == 1:
            thisfile = open(passdir + bugid + '/passcov/' + testname + '/stmt_info.txt')
            thislines = thisfile.readlines()
            thisfile.close()

            thisset = set()
            for i in range(len(thislines)):
                filenameitem = thislines[i].strip().split(':')[0]
                lineitems = thislines[i].strip().split(':')[1].split(',')
                for j in range(len(lineitems)):
                    thisset.add(filenameitem + ':' + lineitems[j])
            existingcovset[testname] = thisset & failset
            unionCovwithFail[testname] = thisset | failset
            passCov[testname] = thisset
            return 0  # different

        thisfile = open(passdir + bugid + '/passcov/' + testname + '/stmt_info.txt')
        thislines = thisfile.readlines()
        thisfile.close()

        thisset = set()
        for i in range(len(thislines)):
            filenameitem = thislines[i].strip().split(':')[0]
            lineitems = thislines[i].strip().split(':')[1].split(',')
            for j in range(len(lineitems)):
                thisset.add(filenameitem + ':' + lineitems[j])

        for key in existingcovset.keys():
            if len(existingcovset[key]) != 0:
                similarity = float(len(existingcovset[key] & (thisset & failset))) / len(
                    existingcovset[key] | (thisset & failset))
                if similarity == 1:
                    return 1  # same
        existingcovset[testname] = thisset & failset
        unionCovwithFail[testname] = thisset | failset
        passCov[testname] = thisset
        return 0  # different

    total_prog = 0
    syn_error_prog = 0
    compiled_prog = 0
    ub_prog = 0
    failing_prog = 0
    passing_interesting = 0
    same_prog = 0
    remove_oracle_prog = 0

    client = OpenAI(
        api_key=key,
        base_url=url
    )

    failProgramPath = baseInfoDir + bugid + '/fail.c'
    program = ""
    actions = []

    with open(failProgramPath, 'r') as file:
        program = file.read()

    with open(actionPath, 'r') as file:
        for line in file:
            parts = line.strip().split(';')
            if len(parts) >= 2:
                actions.append(parts[1].strip())

    starttime = time.time()
    passingcnt = 0
    failcnt = 0
    errorcnt = 0

    # 创建统计文件
    with open('generation_stats.txt', 'w') as stats_file:
        stats_file.write(f"Generation started at: {time.ctime(starttime)}\n")
        stats_file.write(f"Target: {passingcnt}/499 passing cases\n\n")

    while passingcnt < 499:
        endtime = time.time()
        gaptime = endtime - starttime

        # 打印进度
        print(f"\n=== Progress: {passingcnt}/499 passing cases, {total_prog} total generated ===")
        print(f"Elapsed time: {gaptime:.2f}s")

        if gaptime > 3600:
            print("Time limit reached (1 hour)")
            break

        selected = random.choice(actions)
        print(f"actions: {selected}")
        messages = [
            {
                "role": "system",
                "content": "You are an effective program mutator and your job is to generate a semantic in-equivalent variant the input code based on the specified instructions in the following."
            },
        ]

        mutation_instruction = (
            "(*) clear up your memory from now and do not generate repeated programs. "
            "Please generate only one semantic in-equivalent variant of the input test program ```{}``` "
            "(make sure the variant program can be compiled and valid without any undefined behavior) by {} and "
            "keep other code the same as the input program (comment in the code what you have changed)".format(program,
                                                                                                               selected)
        )

        messages.append({"role": "user", "content": mutation_instruction})

        # 添加反馈信息
        if len(ub_set) != 0:
            ub_string = ''
            for ret in ub_set:
                ub_string += ret + ', '
            ub_back_to_llm = "The program you generated is bad, and I got the error \" {} \" from Frama-c. Please do not generate such program again".format(
                ub_string)
            messages.append({"role": "user", "content": ub_back_to_llm})

        syn_string = ''
        if len(syn_err_set) != 0:
            for syn in syn_err_set:
                syn_string += syn + ', '
            syn_back_to_llm = "The program you generated has a syntax error, and I got the error \"{} \" after compiling. Please fix them and do not generate such program again".format(
                syn_string)
            messages.append({"role": "user", "content": syn_back_to_llm})

        try:
            chat_completion = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=1.0,
            )
        except APIError as e:
            print(f"API error: {e}")
            continue
        except APIConnectionError as e:
            print(f"connect error: {e}")
            continue
        except RateLimitError as e:
            print(f"rate limit error: {e}")
            continue
        except Exception as e:
            print(f"other error: {e}")
            continue

        pattern = r"\`\`\`([\s\S]*?)\`\`\`"
        reply = chat_completion.choices[0].message.content
        reply = re.findall(pattern, reply)
        new_reply = []
        for r in reply:
            if r.find('main') != -1:
                new_reply.append(r)
        reply = new_reply

        if len(reply) == 0:
            print("No code block found in response")
            continue

        if reply[-1].startswith('c') or reply[-1].startswith('C'):
            reply[-1] = reply[-1][1:]

        total_prog += 1

        # 保存生成的代码
        with open("mainvar.c", "w") as f:
            for line in reply[-1]:
                f.write(line)

        remove_unused_printf("./main.c", "./mainvar.c")

        print("Generated program:")
        os.system("cat mainvar.c")

        f_mainvar = open('mainvar.c')
        temp = f_mainvar.readlines()
        f_mainvar.close()

        if len(temp) == 0:
            print("None program is generated :(")
            same_prog += 1
            continue

        # Oracle检查
        if check_oracles("./main.c", "mainvar.c") == False:
            print("Checking oracle failed, now try to fix this, ....")
            insert_printf_for_wrong_code_multi('./main.c', './mainvar.c')
            remove_oracle_prog += 1
            if check_oracles("./main.c", "mainvar.c") == False:
                print("Checking oracle still failed, continue to generate a new program ....")
                continue
            else:
                print("Great, the oracle checking is passed!!!")

        print("After oracle checking +++++")
        add_comment_before_string('./mainvar.c')
        print("### End generating program using GPT !")

        # 检查是否与原始程序相同
        if os.path.exists('difftmp'):
            os.system('rm difftmp')
        os.system('diff main.c mainvar.c > difftmp')

        with open('difftmp') as f:
            difflines = f.readlines()

        if len(difflines) == 0:
            same_prog += 1
            print("same_prog : ", same_prog)
            continue

        # 编译和测试验证
        flagIsPass = -1
        err_set = set()
        compilerPath = compilerBasePath + revision + '/' + revision + '-build'
        programPath = workpath + '/mainvar.c'

        try:
            if checkpass == 'checkIsPass_wrongcodeOneline':
                flagIsPass, err_set = checkIsPass_wrongcodeOneline(compilerPath, compileOptionRight, compileOptionWrong,
                                                                   programPath)
            elif checkpass == 'checkIsPass_zeroandsegmentoneline':
                flagIsPass, err_set = checkIsPass_zeroandsegmentoneline(compilerPath, compileOptionRight,
                                                                        compileOptionWrong, programPath)
            elif checkpass == 'checkIsPass_multilineswrongcode':
                flagIsPass, err_set = checkIsPass_multilineswrongcode(compilerPath, compileOptionRight,
                                                                      compileOptionWrong, programPath)
            elif checkpass == 'checkIsPass_zeroandonenumber':
                flagIsPass, err_set = checkIsPass_zeroandonenumber(compilerPath, compileOptionRight, compileOptionWrong,
                                                                   programPath)
            else:
                raise Exception('unknown checkpass')
        except Exception as e:
            print(f"Error during check: {e}")
            flagIsPass = 0
            err_set = {str(e)}

        # 处理测试结果
        if len(err_set) != 0:
            syn_err_set.update(err_set)
            syn_error_prog += 1

        if flagIsPass == 0:
            # 编译错误或其他错误
            errorcnt += 1
            error_filename = f"error_cases/error_{errorcnt:04d}.c"
            shutil.copy('mainvar.c', error_filename)
            print(f"Error case saved as: {error_filename}")

        elif flagIsPass == 1:
            # 测试通过 - 不再触发bug
            passingcnt += 1
            passing_interesting += 1
            compiled_prog += 1

            passcovdir = f"{workpath}/passcov/pass_{passingcnt:04d}"
            os.system('mkdir -p ' + passcovdir)
            collectcov(bugid, revision, passcovdir, configPath)
            covFlag = diffPassCov(f"pass_{passingcnt:04d}", bugid)
            if covFlag == 1:
                os.system(f"rm -rf {passcovdir}")
                passingcnt -= 1
                continue
            else:
                # 保存通过用例
                pass_filename = f"passing_cases/pass_{passingcnt:04d}.c"
                shutil.copy('mainvar.c', pass_filename)
                print(f"PASSING CASE FOUND! Saved as: {pass_filename}")
                print(f"Total passing cases: {passingcnt}/499")
                # 更新统计文件
                with open('generation_stats.txt', 'a') as stats_file:
                    stats_file.write(f"Pass {passingcnt:04d}: {pass_filename} - Mutation: {selected}\n")

        elif flagIsPass == 2:
            # 仍然失败 - 仍然触发bug
            failcnt += 1
            failing_prog += 1
            compiled_prog += 1

            failcovdir = f"{workpath}/failcov/fail_{failcnt:04d}"
            os.system('mkdir -p ' + failcovdir)
            collectcov(bugid, revision, failcovdir, configPath)

            testname = f"fail_{failcnt:04d}"
            covFlag = diffFailCov(testname, bugid)

            if covFlag == 1:
                os.system(f"rm -rf {failcovdir}")
                failcnt -= 1
                failing_prog -= 1
                compiled_prog -= 1
                continue
            else:
                fail_filename = f"failing_cases/fail_{failcnt:04d}.c"
                shutil.copy('mainvar.c', fail_filename)
                with open('generation_stats.txt', 'a') as stats_file:
                    stats_file.write(f"Fail {failcnt:04d}: {fail_filename} - Mutation: {selected}\n")

                print(f"Useful failing case saved as: {fail_filename}")
                print(f"Total useful failing cases: {failcnt}")

        # 定期更新统计信息
        if total_prog % 10 == 0:
            print("\n=== Current Statistics ===")
            print(f"Total generated: {total_prog}")
            print(f"Passing cases: {passingcnt}")
            print(f"Failing cases: {failcnt}")
            print(f"Error cases: {errorcnt}")
            print(f"Same programs: {same_prog}")
            print("========================\n")

    # 生成最终统计报告
    endtime = time.time()
    total_time = endtime - starttime

    with open('generation_stats.txt', 'a') as stats_file:
        stats_file.write(f"\n=== FINAL STATISTICS ===\n")
        stats_file.write(f"Total generation time: {total_time:.2f}s\n")
        stats_file.write(f"Total programs generated: {total_prog}\n")
        stats_file.write(f"Passing cases: {passingcnt}\n")
        stats_file.write(f"Failing cases: {failcnt}\n")
        stats_file.write(f"Error cases: {errorcnt}\n")
        stats_file.write(f"Same programs: {same_prog}\n")
        stats_file.write(f"Syntax error programs: {syn_error_prog}\n")
        stats_file.write(f"Remove oracle programs: {remove_oracle_prog}\n")
        stats_file.write(f"Compiled programs: {compiled_prog}\n")

    print(f"\n=== GENERATION COMPLETED ===")
    print(f"Total time: {total_time:.2f}s")
    print(f"Passing cases: {passingcnt}/499")
    print(f"See generation_stats.txt for details")

    return passingcnt
