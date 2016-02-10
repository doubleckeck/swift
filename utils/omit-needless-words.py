#!/usr/bin/env python

# This tool helps assess the impact of automatically applying
# heuristics that omit 'needless' words from APIs imported from Clang
# into Swift.

from __future__ import print_function

import argparse
import os
import re
import subprocess
import multiprocessing

DEFAULT_TARGET_BASED_ON_SDK = {
    'macosx': 'x86_64-apple-macosx10.11',
    'iphoneos': 'arm64-apple-ios9.0',
    'iphonesimulator': 'x86_64-apple-ios9.0',
    'watchos': 'armv7k-apple-watchos2.0',
    'watchos.simulator': 'i386-apple-watchos2.0',
    'appletvos': 'arm64-apple-tvos9',
    'appletvos.simulator': 'x86_64-apple-tvos9',
}

SKIPPED_FRAMEWORKS = {
    'CalendarStore',
    'CoreMIDIServer',
    'DrawSprocket',
    'DVComponentGlue',
    'InstallerPlugins',
    'InstantMessage',
    'JavaFrameEmbedding',
    'JavaVM',
    'Kerberos',
    'Kernel',
    'LDAP',
    'Message',
    'PCSC',
    'QTKit',
    'Ruby',
    'SyncServices',
    'System',
    'Tk',
    'VideoDecodeAcceleration',
    'vecLib',
}

def create_parser():
    parser = argparse.ArgumentParser(
        description="Determines the effects of omitting 'needless' words from imported APIs",
        prog='omit-needless-words.py',
        usage='python omit-needless-words.py -m AppKit')
    parser.add_argument('-m', '--module', help='The module name.')
    parser.add_argument('-j', '--jobs', type=int, help='The number of parallel jobs to execute')
    parser.add_argument('-s', '--sdk', nargs='+', required=True, help="The SDKs to use.")
    parser.add_argument('-t', '--target', help="The target triple to use.")
    parser.add_argument('-i', '--swift-ide-test', default='swift-ide-test', help="The swift-ide-test executable.")
    parser.add_argument('-3', '--swift-3', action='store_true', help="Use Swift 3 transformation")
    parser.add_argument('-o', '--output-dir', default=os.getcwd(), help='Directory to which the output will be emitted.')
    parser.add_argument('-q', '--quiet', action='store_true', help='Suppress printing of status messages.')
    return parser

def output_command_result_to_file(command_args, filename):
    with open(filename, 'w') as output_file:
        subprocess.call(command_args, stdout=output_file)

def run_command(args):
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    exitcode = proc.returncode
    return (exitcode, out, err)

# Collect the set of submodules for the given module.
def collect_submodules(common_args, module):
    # Execute swift-ide-test to print the interface.
    my_args = ['-module-print-submodules', '-module-to-print=%s' % (module)]
    (exitcode, out, err) = run_command(common_args + my_args)
    if exitcode != 0:
        print('error: submodule collection failed with error %d' % (exitcode))
        return ()

    # Find all of the submodule imports.
    import_matcher = re.compile('.*import\s+%s\.([A-Za-z_0-9.]+)' % (module))
    submodules = set()
    for line in out.splitlines():
        match = import_matcher.match(line)
        if match:
            submodules.add(match.group(1))

    return sorted(list(submodules))

# Dump the API for the given module.
def dump_module_api((cmd, extra_dump_args, output_dir, module, quiet)):
    # Collect the submodules
    submodules = collect_submodules(cmd, module)

    # Dump the top-level module
    subprocess.call(['mkdir', '-p', ('%s/%s' % (output_dir, module))])
    output_file = '%s/%s/%s.swift' % (output_dir, module, module)
    if not quiet:
        print('Writing %s...' % output_file)

    top_level_cmd = cmd + extra_dump_args + ['-module-to-print=%s' % (module)]
    output_command_result_to_file(top_level_cmd, output_file)

    # Dump each submodule.
    for submodule in submodules:
        output_file = '%s/%s/%s.swift' % (output_dir, module, submodule)
        if not quiet:
            print('Writing %s...' % output_file)

        full_submodule = '%s.%s' % (module, submodule)
        submodule_cmd = cmd + extra_dump_args
        submodule_cmd = submodule_cmd + ['-module-to-print=%s' % (full_submodule)]
        output_command_result_to_file(submodule_cmd, output_file)

    return

def pretty_sdk_name(sdk):
    if sdk.find("macosx") == 0:
        return 'OSX'
    if sdk.find("iphoneos") == 0:
        return 'iOS'
    if sdk.find("watchos") == 0:
        return 'watchOS'
    if sdk.find("appletvos") == 0:
        return 'tvOS'
    return 'unknownOS'

# Collect the set of frameworks we should dump
def collect_frameworks(sdk):
    (exitcode, sdk_path, err) = run_command(["xcrun", "--show-sdk-path", "-sdk", sdk])
    if exitcode != 0:
        print('error: framework collection failed with error %d' % (exitcode))
        return ()
    sdk_path = sdk_path.rstrip()

    (exitcode, sdk_version, err) = run_command(["xcrun", "--show-sdk-version", "-sdk", sdk])
    if exitcode != 0:
        print('error: framework collection failed with error %d' % (exitcode))
        return ()
    sdk_version = sdk_version.rstrip()

    print('Collecting frameworks from %s %s at %s' % (pretty_sdk_name(sdk), sdk_version, sdk_path))

    # Collect all of the framework names
    frameworks_dir = '%s/System/Library/Frameworks' % sdk_path
    framework_matcher = re.compile('([A-Za-z_0-9.]+)\.framework')
    frameworks = set()
    for entry in os.listdir(frameworks_dir):
        match = framework_matcher.match(entry)
        if match:
            framework = match.group(1)
            if framework not in SKIPPED_FRAMEWORKS:
                frameworks.add(framework)

    return (sorted(list(frameworks)), sdk_path)

def create_dump_module_api_args(cmd_common, cmd_extra_args, sdk, module, target, source_filename, output_dir, quiet):

    # Determine the SDK root and collect the set of frameworks.
    (frameworks, sdk_root) = collect_frameworks(sdk)

    # Determine the default target.
    if target:
        sdk_target = target
    else:
        sdk_target = DEFAULT_TARGET_BASED_ON_SDK[sdk]

    # Determine the output idirectory
    pretty_sdk = pretty_sdk_name(sdk)
    sdk_output_dir = '%s/%s' % (output_dir, pretty_sdk)

    # Create the sets of arguments to dump_module_api.
    results = []
    cmd = cmd_common + ['-sdk', sdk_root, '-target', sdk_target]
    if module:
        results.append((cmd, cmd_extra_args, sdk_output_dir, module, quiet))
    else:
        for framework in frameworks:
            results.append((cmd, cmd_extra_args, sdk_output_dir, framework, quiet))

    return results

def main():
    source_filename = 'omit-needless-words.swift'
    parser = create_parser()
    args = parser.parse_args()

    cmd_common = [args.swift_ide_test, '-print-module', '-source-filename', source_filename, '-module-print-skip-overlay', '-skip-unavailable', '-skip-print-doc-comments']

    # Determine the set of extra arguments we'll use.
    extra_args = ['-skip-imports']
    if args.swift_3:
        extra_args = extra_args + ['-enable-omit-needless-words', '-enable-infer-default-arguments', '-enable-strip-ns-prefix']

    # Create a .swift file we can feed into swift-ide-test
    subprocess.call(['touch', source_filename])

    # Construct the set of API dumps we should perform.
    jobs = []
    for sdk in args.sdk:
        jobs = jobs + create_dump_module_api_args(cmd_common, extra_args, sdk, args.module, args.target, source_filename, args.output_dir, args.quiet)

    # Execute the API dumps
    pool = multiprocessing.Pool(processes=args.jobs)
    pool.map(dump_module_api, jobs)

    # Remove the .swift file we fed into swift-ide-test
    subprocess.call(['rm', '-f', source_filename])

if __name__ == '__main__':
    main()
