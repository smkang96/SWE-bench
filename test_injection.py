import subprocess as sp
import os
import json
import difflib
import tqdm
import argparse

REPO_DIR = './repos/'
INJECT_ID_STR = 'AUTOINJECTED'
EXAMPLE_TEST = f'''
def test_failure_assured_{INJECT_ID_STR}():
    assert False
'''
INDENT_EXAMPLE_TEST = '\n'.join('    '+line for line in EXAMPLE_TEST.split('\n'))

MODEL_NAME = 'doctester'

def initialize_repo(repo_name, commit_hash):
    dir_name = repo_name.split('/')[-1]
    target_repo_dir = os.path.join(REPO_DIR, dir_name)
    if not os.path.isdir(target_repo_dir):
        p = sp.run(['git', 'clone', 'https://github.com/' + repo_name], stdout=sp.DEVNULL, cwd=REPO_DIR)
        assert p.returncode == 0, f'git clone failed for {repo_name}.'
    p = sp.run(['git', 'reset', '--hard', 'HEAD'], stdout=sp.DEVNULL, cwd=target_repo_dir)
    assert p.returncode == 0, f'git reset failed for {repo_name}.'
    p = sp.run(['git', 'checkout', commit_hash], stdout=sp.DEVNULL, stderr=sp.DEVNULL, cwd=target_repo_dir)
    assert p.returncode == 0, f'git checkout failed for {repo_name}, hash={commit_hash}.'
    return target_repo_dir

def get_test_file_and_content(repo_dir, test_patch_content):
    changed_files = [e.removeprefix('--- a/')
                     for e in test_patch_content.splitlines() if e.startswith('---')]

    for local_file_path in changed_files:
        full_file_path = os.path.join(repo_dir, local_file_path)
        with open(full_file_path) as f:
            file_content = f.read()
            return local_file_path, file_content
    else:
        raise ValueError('Test file was not found.')

def get_test_diff(test_file_path, test_file_content, target_test):
    test_file_lines = test_file_content.splitlines()
    new_test_file_lines = test_file_lines + [''] + target_test.splitlines()
    test_diff = difflib.unified_diff(
        test_file_lines, 
        new_test_file_lines, 
        fromfile='a/'+test_file_path,
        tofile='b/'+test_file_path,
        lineterm=''
    )
    git_diff_preamble = f'diff --git a/{test_file_path} b/{test_file_path}\n'
    return git_diff_preamble+'\n'.join(test_diff)+'\n'

def generate_patch_object(instance_id, test_diff):
    return {
        'instance_id': instance_id,
        'model_patch': test_diff,
        'model_name_or_path': MODEL_NAME,
    }

def save_patch_objects(patch_objects, save_path):
    with open(save_path, 'w') as f:
        for patch_obj in patch_objects:
            print(json.dumps(patch_obj), file=f)

def run_test_evaluation(save_path, run_id='dev-run'):
    p = sp.run(['python3.9', '-m', 'swebench.harness.run_evaluation',
                '--predictions_path', save_path,
                '--max_workers', '1',
                '--run_id', run_id])
    assert p.returncode == 0, 'test evaluation failed.'

def retrieve_test_results(run_id, instance_id):
    log_dir = os.path.join('logs/run_evaluation', run_id, MODEL_NAME, instance_id)
    test_output = os.path.join(log_dir, 'test_output.txt')
    injected_test_found = False
    with open(test_output) as f:
        for line in f:
            if INJECT_ID_STR in line:
                injected_test_found = True
                if '::' in line:
                    assert any(res in line for res in ('PASSED', 'FAILED'))
                    return line.split()[0] == 'PASSED'
                elif ' ... ' in line:
                    assert any(res in line for res in ('ok', 'ERROR'))
                    return line.strip().split()[-1] == 'ok'
                elif line.strip().endswith(' ok') or line.strip().endswith(' [FAIL]'):
                    assert instance_id.split('__')[0] == 'sympy' # only sure for sympy, need to check elsewise
                    return line.strip().split()[-1] == 'ok'
        else:
            if injected_test_found:
                raise ValueError(f'Potentially unknown test framework for {instance_id}')
            else:
                raise ValueError(f'Injected test not found for {instance_id}')

def run_test(bug_data, injecting_test, run_id):
    initialize_repo(bug_data['repo'], bug_data['base_commit'])
    test_file, test_file_content = get_test_file_and_content(
        os.path.join(REPO_DIR, os.path.basename(bug_data['repo'])), 
        bug_data['test_patch']
    )
    test_diff = get_test_diff(test_file, test_file_content, injecting_test)
    patch_obj = generate_patch_object(bug_data['instance_id'], test_diff)

    save_path = f'{MODEL_NAME}/example.jsonl'
    
    save_patch_objects([patch_obj], f'{MODEL_NAME}/example.jsonl')
    run_test_evaluation(save_path, run_id)
    test_exec_result = retrieve_test_results(run_id, example_data['instance_id'])
    return test_exec_result
    
        
if __name__ == '__main__':
    from datasets import load_dataset

    parser = argparse.ArgumentParser()
    parser.add_argument('--expr_name', default='test')
    args = parser.parse_args()

    test_bench = load_dataset('princeton-nlp/SWE-bench_Lite', split='test')
    test_results = dict()
    run_id = args.expr_name
    for example_data in tqdm.tqdm(test_bench):
        project_name = example_data['instance_id'].split('__')[0]
        if project_name != 'sympy':
            continue
        print(example_data['instance_id'])
        try:
            if project_name in ('django',):
                test_exec_result = run_test(example_data, INDENT_EXAMPLE_TEST, run_id)
            else:
                test_exec_result = run_test(example_data, EXAMPLE_TEST, run_id)
            ran_successfully = True
        except Exception as e:
            ran_successfully = False
            test_exec_result = 'in-execution error: ' + repr(e)
        test_results[example_data['instance_id']] = {
            EXAMPLE_TEST: {
                'executable': ran_successfully,
                'passed': test_exec_result
            }
        }
        with open(run_id+'.json', 'w') as f:
            json.dump(test_results, f)
    print('a-ok')
    
    
