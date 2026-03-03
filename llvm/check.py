import os, time
import re


# compilerPath = '.../r181189-build'
def checkIsPass_wrongcodeOneline(compilerPath, compilerOptionRight, compilerOptionWrong, programPath):
    err_set = set()
    gccPath = compilerPath + '/bin/clang'
    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find ' + compilerPath + ' -name \"*.gcda\" | xargs rm -f')
    os.system('{ ' + gccPath + ' ' + compilerOptionRight + ' ' + programPath + ' ;} >comp_output.txt 2>&1')
    os.system("cat comp_output.txt")
    lines = ''
    with open('comp_output.txt', 'r') as file:
        for line in file:
            lines += line
    regex = re.compile(r'^.*\berror\w*\b.*$', re.MULTILINE)
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    regex = re.compile(r'^.*\bundefined\w*\b.*$', re.MULTILINE)
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    if not os.path.exists('a.out'):
        return 0, err_set  # compilation error
    if os.path.exists('rightfile'):
        os.system('rm rightfile')
    start = time.time()
    os.system('{ timeout 10 ./a.out ; } >rightfile 2>&1')
    end = time.time()
    if (end - start) >= 10:
        return 0, err_set
    f = open('rightfile')
    lines = f.readlines()
    f.close()
    if len(lines) != 1:
        return 0, err_set
    else:
        if 'core dumped' in lines[0] or 'dumped core' in lines[0]:
            return 0, err_set
    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find ' + compilerPath + ' -name \"*.gcda\" | xargs rm -f')
    os.system(gccPath + ' ' + compilerOptionWrong + ' ' + programPath)
    if not os.path.exists('a.out'):
        return 0, err_set
    if os.path.exists('wrongfile'):
        os.system('rm wrongfile')
    start = time.time()
    os.system('{ timeout 10 ./a.out ; } >wrongfile 2>&1')
    end = time.time()
    if (end - start) >= 10:
        return 0, err_set
    f = open('wrongfile')
    lines = f.readlines()
    f.close()
    if len(lines) != 1:
        return 0, err_set
    else:
        if 'core dumped' in lines[0] or 'dumped core' in lines[0]:
            return 0, err_set
    if os.path.exists('diffwr'):
        os.system('rm diffwr')
    os.system('diff wrongfile rightfile > diffwr')
    f = open('diffwr')
    lines = f.readlines()
    f.close()
    if len(lines) == 0:
        return 1, err_set
    else:
        return 2, err_set


def checkIsPass_zeroandsegmentoneline(compilerPath, compilerOptionRight, compilerOptionWrong,
                                      programPath):  # change per bug
    err_set = set()
    gccPath = compilerPath + '/bin/clang'
    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find ' + compilerPath + ' -name \"*.gcda\" | xargs rm -f')
    os.system('{ ' + gccPath + ' ' + compilerOptionRight + ' ' + programPath + ' ;} >comp_output.txt 2>&1')
    os.system("cat comp_output.txt")
    lines = ''
    with open('comp_output.txt', 'r') as file:
        for line in file:
            lines += line
    regex = re.compile(r'^.*\berror\w*\b.*$', re.MULTILINE)
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    regex = re.compile(r'^.*\bundefined\w*\b.*$', re.MULTILINE)
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    if not os.path.exists('a.out'):
        return 0, err_set

    if os.path.exists('rightfile'):
        os.system('rm rightfile')

    start = time.time()
    os.system('{ timeout 10 ./a.out ; } >rightfile 2>&1')
    end = time.time()
    if (end - start) >= 10:
        return 0, err_set

    f = open('rightfile')
    lines = f.readlines()
    f.close()
    if len(lines) != 0:
        return 0, err_set

    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find ' + compilerPath + ' -name \"*.gcda\" | xargs rm -f')
    os.system(gccPath + ' ' + compilerOptionWrong + ' ' + programPath)
    if not os.path.exists('a.out'):
        return 0, err_set

    if os.path.exists('wrongfile'):
        os.system('rm wrongfile')
    start = time.time()
    os.system('{ timeout 10 ./a.out ; } >wrongfile 2>&1')
    end = time.time()
    if (end - start) >= 10:
        return 0, err_set

    f = open('wrongfile')
    lines = f.readlines()
    f.close()

    if os.path.exists('diffwr'):
        os.system('rm diffwr')
    os.system('diff wrongfile rightfile > diffwr')
    f = open('diffwr')
    diffmesslines = f.readlines()
    f.close()

    if os.path.exists('diffow'):
        os.system('rm diffow')
    os.system('diff wrongfile oriwrongfile > diffow')
    f = open('diffow')
    diffowlines = f.readlines()
    f.close()

    if len(diffmesslines) == 0:
        return 1, err_set  # pass
    else:
        if len(diffowlines) == 0:  # 'core dumped' in lines[0]:
            return 2, err_set  # still fail
        else:
            return 0, err_set


def checkIsPass_multilineswrongcode(compilerPath, compilerOptionRight, compilerOptionWrong,
                                    programPath):  # change per bug
    err_set = set()
    gccPath = compilerPath + '/bin/clang'
    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find ' + compilerPath + ' -name \"*.gcda\" | xargs rm -f')
    os.system('{ ' + gccPath + ' ' + compilerOptionRight + ' ' + programPath + ' ;} >comp_output.txt 2>&1')
    os.system("cat comp_output.txt")
    lines = ''
    with open('comp_output.txt', 'r') as file:
        for line in file:
            lines += line
    regex = re.compile(r'^.*\berror\w*\b.*$', re.MULTILINE)
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    regex = re.compile(r'^.*\bundefined\w*\b.*$', re.MULTILINE)
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    if not os.path.exists('a.out'):
        return 0, err_set
    if os.path.exists('rightfile'):
        os.system('rm rightfile')
    start = time.time()
    os.system('{ timeout 10 ./a.out ; } >rightfile 2>&1')
    end = time.time()
    if (end - start) >= 10:
        return 0, err_set
    f = open('rightfile')
    lines = f.readlines()
    f.close()
    if len(lines) <= 1:
        return 0, err_set
    if 'core dumped' in lines[0] or 'dumped core' in lines[0] or 'exception' in lines[0] or 'Abort' in lines[
        0] or 'Segmentation' in lines[0]:
        return 0, err_set
    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find ' + compilerPath + ' -name \"*.gcda\" | xargs rm -f')
    os.system(gccPath + ' ' + compilerOptionWrong + ' ' + programPath)
    if not os.path.exists('a.out'):
        return 0, err_set
    if os.path.exists('wrongfile'):
        os.system('rm wrongfile')
    start = time.time()
    # os.system('timeout 10 ./a.out 2>&1 | tee wrongfile')
    os.system('{ timeout 10 ./a.out ; } >wrongfile 2>&1')
    end = time.time()
    if (end - start) >= 10:
        return 0, err_set
    f = open('wrongfile')
    lines = f.readlines()
    f.close()
    if len(lines) <= 1:
        return 0, err_set
    if 'core dumped' in lines[0] or 'dumped core' in lines[0] or 'exception' in lines[0] or 'Abort' in lines[
        0] or 'Segmentation' in lines[0]:
        return 0, err_set
    if os.path.exists('diffwr'):
        os.system('rm diffwr')
    os.system('diff wrongfile rightfile > diffwr')
    f = open('diffwr')
    diffmesslines = f.readlines()
    f.close()
    if len(diffmesslines) == 0:
        return 1, err_set  # pass
    else:
        return 2, err_set  # still fail


def checkIsPass_zeroandonenumber(compilerPath, compilerOptionRight, compilerOptionWrong, programPath):  # change per bug
    err_set = set()
    gccPath = compilerPath + '/bin/clang'
    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find ' + compilerPath + ' -name \"*.gcda\" | xargs rm -f')
    os.system('{ ' + gccPath + ' ' + compilerOptionRight + ' ' + programPath + ' ;} >comp_output.txt 2>&1')
    os.system("cat comp_output.txt")
    lines = ''
    with open('comp_output.txt', 'r') as file:
        for line in file:
            lines += line
    regex = re.compile(r'^.*\berror\w*\b.*$', re.MULTILINE)
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    regex = re.compile(r'^.*\bundefined\w*\b.*$', re.MULTILINE)
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    if not os.path.exists('a.out'):
        return 0, err_set
    if os.path.exists('rightfile'):
        os.system('rm rightfile')
    start = time.time()
    os.system('{ timeout 10 ./a.out ; } >rightfile 2>&1')


    end = time.time()
    if (end - start) >= 10:
        return 0, err_set
    f = open('rightfile')
    lines = f.readlines()
    f.close()
    if len(lines) != 0:
        return 0, err_set
    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find ' + compilerPath + ' -name \"*.gcda\" | xargs rm -f')
    os.system(gccPath + ' ' + compilerOptionWrong + ' ' + programPath)
    if not os.path.exists('a.out'):
        return 0, err_set
    if os.path.exists('wrongfile'):
        os.system('rm wrongfile')
    start = time.time()
    # os.system('timeout 10 ./a.out 2>&1 | tee wrongfile')
    os.system('{ timeout 10 ./a.out ; } >wrongfile 2>&1')
    end = time.time()
    if (end - start) >= 10:
        return 0, err_set
    f = open('wrongfile')
    lines = f.readlines()
    f.close()
    if os.path.exists('diffwr'):
        os.system('rm diffwr')
    os.system('diff wrongfile rightfile > diffwr')
    f = open('diffwr')
    diffmesslines = f.readlines()
    f.close()
    if len(diffmesslines) == 0:
        return 1, err_set  # pass
    else:
        if len(lines) == 1 and 'core dumped' not in lines[0] and 'dumped core' not in lines[0]:
            return 2, err_set  # still fail
        else:
            return 0, err_set
