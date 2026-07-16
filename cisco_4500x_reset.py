# $language = "python"
# $interface = "1.0"
"""
cisco_4500x_reset.py
Andrew Boakye

Automates a full factory reset of a Cisco Catalyst 4500-X (WS-C4500X) switch
via SecureCRT console session. These switches run classic Cisco IOS -- NOT
IOS-XE (like the C9200/3850) and NOT NX-OS (like the Nexus 9000).

WHY THIS IS A DIFFERENT SCRIPT FROM BOTH cisco_3850_reset.py AND cisco_n9k_reset.py
------------------------------------------------------------------------------------
* ROMMON prompt is the classic "rommon 1 >" style (numbered, increments
  each command), NOT the "switch:" prompt used by the newer unified
  bootloader on C9200/3850, and NOT the Nexus "loader >" prompt.

* To ignore startup-config on next boot, classic ROMMON uses the
  config-register mechanism: "confreg 0x2142". This is the same
  password-recovery value used across classic Cisco IOS routers/switches.
  Restoring the DEFAULT register value (0x2102) before the final reload is
  required, or the switch will keep ignoring startup-config forever.

* Login credentials may be unknown. Unlike the N9K, classic IOS does NOT
  force a mandatory new admin password after an erase -- once booted while
  ignoring startup-config, "enable" reaches a privileged prompt with no
  password at all.

* The VLAN database (vlan.dat) may live on a separate filesystem
  historically named "cat4000_flash:" on this hardware family, distinct
  from "bootflash:"/"flash:" where the IOS image itself lives.
  variables['vlan_directory'] is separate from variables['directory']
  for exactly this reason.

IMPORTANT NOTES FROM REAL-WORLD TESTING
------------------------------------------------------------------------------------
* ROMMON on this hardware (Rom Monitor 15.0(1r)SG12) rejects 'confreg 0x2142'
  as a direct argument -- the config register can only be set via the
  interactive wizard. See bypass_startup_config().

* On switches configured for VSS mode (Virtual Switching System), booting
  with startup-config ignored is not allowed -- the switch deliberately
  self-reloads back to ROMMON. This is fixed by clearing the
  VS_SWITCH_NUMBER ROMMON variable to force standalone mode first.

* Bare '>' / '#' are NOT safe match strings on this platform. The
  bootloader's own image-loading progress bar outputs literal '#'
  characters, so matching on a bare '#' can trigger long before the switch
  has actually finished booting. This script matches on the actual hostname
  prompt (variables['hostname'] + '>'/'#') instead, everywhere that
  detects the post-boot prompt.

* Bulk wildcard deletes (e.g. matching many files at once with a pattern)
  are confirmed to crash this switch back to rommon. clean_directory()
  deletes one exact filename at a time instead.

* With keep_patterns left empty, clean_directory() will delete the switch's
  own boot image, leaving it unbootable. keep_patterns defaults to ['.bin'],
  and clean_directory() ALSO hard-codes '.bin' protection directly in code
  so this cannot happen regardless of config state.

IMPORTANT -- VALIDATE IN A LAB FIRST
------------------------------------------------------------------------------------
Behavior differences exist across IOS releases and hardware revisions.
This script logs everything to 4500x_log.txt, including full screen dumps
whenever a wait times out. Check the log after each run and adjust as needed.

This script assumes you are attached to the console (not SSH), since it
has to survive reloads and boot cycles.
"""

from datetime import datetime
import json
import os
import time

variables = {}
objTab = crt.GetScriptTab()
objTab.Screen.Synchronous = True
objTab.Screen.IgnoreEscape = True
end_line = chr(13)


def main():
    global variables
    variables = load_variable_file()
    log_message("\n\n***********************************************************************************")
    log_message("Starting Catalyst 4500-X Reset Process: " + str(datetime.now()))
    log_message('Log File Directory: ' + get_log_path())
    handle_device()
    log_message('Ending Catalyst 4500-X Reset Process.')
    log_message("***********************************************************************************\n\n")


# ====================================================================================================================
# Config / logging
# ====================================================================================================================

def load_variable_file():
    script_directory = os.path.dirname(os.path.abspath(__file__))
    variables_path = os.path.join(script_directory, "4500x_variables.json")
    if not os.path.exists(variables_path):
        log_message('load_variable_file: No variable file detected.')
        default_data = {
            "rommon_prompt": "rommon",
            "directory": "bootflash:",
            "vlan_directory": "cat4000_flash:",
            "keep_patterns": [".bin"],
            "protected_names": [],
            "setup_dialog_prompt": "[yes/no]:",
            "login_prompt": "login:",
            "hostname": "Switch"   # <-- Set to match your switch's actual hostname
        }
        with open(variables_path, 'a') as file:
            json.dump(default_data, file, indent=4)
        log_message('load_variable_file: Variables file created! {}'.format(variables_path))
    with open(variables_path, 'r') as file:
        log_message('load_variable_file: Reading variables from file: {}'.format(variables_path))
        return json.load(file)


def log_message(message):
    with open(get_log_path(), "a") as file:
        file.write(message + '\n')


def display_to_user(message):
    crt.Dialog.MessageBox(message)


def get_log_path():
    script_directory = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_directory, "4500x_log.txt")


# ====================================================================================================================
# Screen reading helpers
# ====================================================================================================================

def read_screen_range(start_row, end_row=None):
    """
    Read screen rows [start_row, end_row] (inclusive, 1-indexed), joined
    with real newlines. Screen.Get() does not insert newlines between rows
    on its own, and CurrentRow tracks an absolute, ever-growing position in
    the session's scrollback (not just what's in the visible viewport).
    """
    if end_row is None:
        end_row = objTab.Screen.CurrentRow
    try:
        lines = []
        for row in range(start_row, end_row + 1):
            lines.append(objTab.Screen.Get(row, 1, row, objTab.Screen.Columns))
        return '\n'.join(lines)
    except Exception as e:
        return '(could not read screen: {})'.format(str(e))


def dump_screen_for_debug(label, last_n_rows=150):
    end_row = objTab.Screen.CurrentRow
    start_row = max(1, end_row - last_n_rows + 1)
    log_message('dump_screen_for_debug [{}]:\n{}'.format(label, read_screen_range(start_row, end_row)))


def run_command_capture(command, timeout=45, last_n_rows=150, allow_recovery=True, settle_seconds=5):
    """
    Send `command`, wait for the prompt via WaitForStrings (advancing past
    any '--More--' pagination along the way), pause `settle_seconds` for
    rendering to settle, then read back the last `last_n_rows` rows.

    settle_seconds defaults to 5, tuned for slow-rendering commands like
    'dir' with many files. Fast commands can pass a smaller value.

    If the current prompt after the command looks like rommon (indicating
    the command crashed the switch), this re-drives the recovery sequence
    and retries the command once. Only checks the last non-blank line
    (the actual current prompt), not the whole window, to avoid false
    positives from stale rommon text still visible in the scrollback.
    """
    objTab.Screen.Send(command + end_line)
    for _ in range(20):
        index = objTab.Screen.WaitForStrings(['--More--', '#'], timeout)
        if index == 1:
            objTab.Screen.Send(' ')
            continue
        break

    if index == 0:
        dump_screen_for_debug('run_command_capture: "{}" timed out'.format(command))
        return ''

    time.sleep(settle_seconds)
    end_row = objTab.Screen.CurrentRow
    start_row = max(1, end_row - last_n_rows + 1)
    text = read_screen_range(start_row, end_row)

    last_line = ''
    for line in reversed(text.split('\n')):
        if line.strip():
            last_line = line.strip().lower()
            break

    if allow_recovery and 'rommon' in last_line:
        log_message('run_command_capture: "{}" -- current prompt is "{}", device crashed back to '
                    'rommon, recovering and retrying once.'.format(command, last_line))
        recover_from_unexpected_reset()
        return run_command_capture(command, timeout=timeout, last_n_rows=last_n_rows,
                                   allow_recovery=False, settle_seconds=settle_seconds)
    return text


def recover_from_unexpected_reset():
    """
    Re-drive the same recovery sequence used at the very start of
    handle_device() when a command unexpectedly crashes the switch back
    to rommon mid-session.

    Waits for the rommon prompt to actually reappear and settle before
    attempting any commands -- sending input too early (while the device
    is still resetting) causes commands to be lost entirely.
    """
    index = objTab.Screen.WaitForStrings([variables['rommon_prompt']], 60)
    if index == 0:
        dump_screen_for_debug('recover_from_unexpected_reset: timed out waiting for rommon to reappear')
    time.sleep(5)
    bypass_startup_config()
    wait_for_unauthenticated_prompt()
    enable_no_password()
    time.sleep(3)
    disable_pagination()
    time.sleep(3)


# ====================================================================================================================
# ROMMON / boot handling
# ====================================================================================================================

def bypass_startup_config():
    """
    Use the classic IOS ROMMON config-register mechanism to make the switch
    boot while ignoring startup-config. The config register can only be
    changed through the interactive wizard ('confreg' with no argument) --
    'confreg 0x2142' as a direct argument is rejected on this ROMMON build.

    The wizard's wording flips between "enable X?" (X is currently OFF) and
    "disable X?" (X is currently ON) depending on the current state. This
    function reads each question's actual text and answers based on intent:
    - "ignore system config info" should end up ON
    - Everything else should end up OFF/default
    - The two outer gates ("change/save the configuration?") always need 'y'

    Also clears the VS_SWITCH_NUMBER ROMMON variable to force standalone
    mode on switches configured for VSS -- VSS mode does not allow booting
    with startup-config ignored and will self-reload back to ROMMON if not
    cleared first.

    Assumes the device is already at a rommon prompt (interrupted during
    boot with Ctrl+C / Ctrl+Break).
    """
    objTab.Screen.Send('confreg' + end_line)
    retried_confreg = False

    for i in range(20):
        index = objTab.Screen.WaitForStrings(['[n]:', variables['rommon_prompt']], 30)
        if index == 0:
            if i == 0 and not retried_confreg:
                log_message('bypass_startup_config: no response to initial "confreg", resending once.')
                dump_screen_for_debug('bypass_startup_config: no response before resend')
                objTab.Screen.Send('confreg' + end_line)
                retried_confreg = True
                continue
            dump_screen_for_debug('bypass_startup_config: confreg wizard timed out')
            display_to_user('The confreg wizard did not respond as expected. Check 4500x_log.txt.')
            raise Exception('Timed out in confreg wizard.')
        if index == 2:
            break

        end_row = objTab.Screen.CurrentRow
        window = read_screen_range(max(1, end_row - 5), end_row)
        question_line = ''
        for line in reversed(window.split('\n')):
            if '[n]:' in line:
                question_line = line.lower()
                break

        if 'change the configuration' in question_line or 'save this configuration' in question_line:
            answer = 'y'
        elif 'change console baud rate' in question_line or 'change the boot characteristics' in question_line:
            answer = 'n'
        elif 'ignore system config info' in question_line:
            answer = 'y' if 'enable' in question_line else 'n'
        else:
            answer = 'n' if 'enable' in question_line else 'y'

        objTab.Screen.Send(answer + end_line)
        log_message('bypass_startup_config: "{}" -> answered {}'
                    .format(question_line if question_line else '(blank)', answer))

    log_message('bypass_startup_config: Drove confreg wizard adaptively, '
                'targeting "ignore system config info" = ON.')

    objTab.Screen.Send('unset VS_SWITCH_NUMBER' + end_line)
    objTab.Screen.WaitForStrings([variables['rommon_prompt']], 30)
    log_message('bypass_startup_config: Cleared VS_SWITCH_NUMBER to force standalone mode.')

    objTab.Screen.Send('boot' + end_line)
    log_message('bypass_startup_config: Sent boot command.')


def wait_for_unauthenticated_prompt():
    """
    Wait for the device to finish booting with startup-config ignored.
    Classic IOS with confreg 0x2142 lands at an unauthenticated "Switch>"
    (user EXEC) prompt with no login needed, possibly after a setup dialog
    (declined here).

    Matches on the actual hostname prompt ("Switch>"/"Switch#") rather than
    bare '>'/'#' -- the bootloader's own image-loading progress bar outputs
    literal '#' characters, which can satisfy a bare '#' match long before
    the switch has finished booting. Polled in short chunks so the log shows
    continuous progress on a boot that can take several minutes.
    """
    hostname = variables['hostname']
    poll_seconds = 15
    total_timeout = 600
    elapsed = 0
    index = 0

    while elapsed < total_timeout:
        index = objTab.Screen.WaitForStrings(
            [variables['setup_dialog_prompt'], variables['login_prompt'],
             hostname + '>', hostname + '#'], poll_seconds)
        elapsed += poll_seconds
        if index != 0:
            break
        log_message('wait_for_unauthenticated_prompt: still waiting, ~{}s elapsed...'.format(elapsed))
        dump_screen_for_debug('wait_for_unauthenticated_prompt: {}s poll'.format(elapsed))

    if index == 0:
        dump_screen_for_debug('wait_for_unauthenticated_prompt: timed out')
        display_to_user('Boot did not reach a usable prompt within timeout. Check 4500x_log.txt.')
        raise Exception('Timed out waiting for boot to complete.')

    dump_screen_for_debug('wait_for_unauthenticated_prompt: reached prompt (index {})'.format(index))

    if index == 1:
        objTab.Screen.Send('no' + end_line)
        objTab.Screen.WaitForStrings([hostname + '>', hostname + '#'], 30)
        log_message('wait_for_unauthenticated_prompt: Declined initial configuration dialog.')
    elif index == 2:
        display_to_user('Device asked for a login even with startup-config ignored -- '
                        'check 4500x_log.txt; this needs investigation.')
        raise Exception('Unexpected login prompt after confreg bypass.')

    log_message('wait_for_unauthenticated_prompt: Reached prompt.')


def enable_no_password():
    """
    With startup-config ignored, there should be no enable secret loaded,
    so 'enable' should go straight to a privileged '#' prompt with no
    password requested. Matches on the actual hostname prompt, not a bare
    '#' -- see wait_for_unauthenticated_prompt() for why.
    """
    hostname = variables['hostname']
    objTab.Screen.Send('enable' + end_line)
    index = objTab.Screen.WaitForStrings(['Password:', hostname + '#'], 30)
    if index == 1:
        display_to_user('Device asked for an enable password even with startup-config ignored -- '
                        'check 4500x_log.txt; this needs investigation.')
        raise Exception('Unexpected enable password prompt after confreg bypass.')
    log_message('enable_no_password: Reached privileged EXEC prompt.')


def disable_pagination():
    """
    Classic IOS paginates long output with '--More--' by default.
    'terminal length 0' disables pagination for the session.
    Matches on the hostname prompt, not a bare '#'.
    """
    objTab.Screen.Send('terminal length 0' + end_line)
    objTab.Screen.WaitForString(variables['hostname'] + '#')
    log_message('disable_pagination: Sent terminal length 0.')


def restore_config_register():
    """
    Set the config register back to the normal default (0x2102) so the
    switch boots normally (reads startup-config) from now on. Required --
    without this the switch would ignore startup-config on every future
    boot, which would look broken to whoever uses it next.
    """
    objTab.Screen.Send('conf t' + end_line)
    objTab.Screen.WaitForString('#')
    objTab.Screen.Send('config-register 0x2102' + end_line)
    objTab.Screen.WaitForString('#')
    objTab.Screen.Send('end' + end_line)
    objTab.Screen.WaitForString('#')
    log_message('restore_config_register: Set config-register back to 0x2102.')


# ====================================================================================================================
# Bootflash / config cleanup
# ====================================================================================================================

def parse_directory_listing(text):
    """
    Classic IOS 'dir' rows look like:
      12  -rwx  1234567  Jan 1 2020 12:00:00 +00:00  filename.bin

    The first token is an index number (not a byte size like on the N9K).
    Only rows starting with a digit are treated as real entries.
    """
    files = []
    for row in text.split('\n'):
        row = row.strip()
        if not row:
            continue
        entries = row.split()
        if not entries:
            continue
        if not entries[0].isdigit():
            continue
        if 'bytes' in row:
            continue
        name = entries[-1]
        if name.endswith('/'):
            name = name[:-1]
        if name in ('.', '..'):
            continue
        files.append(name)
    return files


def get_directory_contents(directory, max_attempts=3):
    """
    Capture a 'dir' listing with retries in case the first attempt returns
    stale/leftover text before the command has fully rendered.
    """
    for attempt in range(1, max_attempts + 1):
        text = run_command_capture('dir {}'.format(directory))
        log_message('get_directory_contents: attempt {} dir output:\n{}'.format(attempt, text))
        files = parse_directory_listing(text)
        if files:
            log_message('get_directory_contents: Parsed files: {}'.format(files))
            return files
        log_message('get_directory_contents: attempt {} found no real file entries, '
                    'retrying after a longer pause.'.format(attempt))
        time.sleep(3 * attempt)

    log_message('get_directory_contents: WARNING - never captured a real listing after {} attempts.'
                .format(max_attempts))
    return []


def clean_directory(directory, files):
    """
    Delete every entry in `files`, one exact filename at a time.

    Bulk wildcard deletes are confirmed to crash this switch back to rommon,
    so this deliberately deletes one file at a time instead.

    '.bin' is hard-coded as a non-negotiable protection in addition to
    keep_patterns from config.
    """
    keep_patterns = set(variables['keep_patterns']) | {'.bin'}
    protected_names = variables['protected_names']

    for name in files:
        if any(pattern in name for pattern in keep_patterns):
            log_message('clean_directory: Skipping (matches keep_patterns) {}'.format(name))
            continue
        if name in protected_names:
            log_message('clean_directory: Skipping protected entry {}'.format(name))
            continue

        target = '{}{}'.format(directory, name)
        log_message('clean_directory: Deleting {}'.format(target))
        text = run_command_capture('delete /force {}'.format(target), settle_seconds=1)
        log_message('clean_directory: Response for {}:\n{}'.format(target, text))
        if '[confirm]' in text:
            objTab.Screen.Send(end_line)
            objTab.Screen.WaitForString('#')
        log_message('clean_directory: Done with {}'.format(target))
        time.sleep(1)


def erase_vlan_database():
    """
    Wipe the VLAN database using 'erase cat4000_flash:' -- confirmed as the
    correct command for this hardware family. VLANs live on cat4000_flash:,
    not in startup-config, even in VTP Transparent mode.
    """
    objTab.Screen.Send('erase {}'.format(variables['vlan_directory']) + end_line)
    log_message('erase_vlan_database: Sent erase {}'.format(variables['vlan_directory']))
    index = objTab.Screen.WaitForStrings(['[confirm]', 'Continue?', '#'], 30)
    if index in (1, 2):
        objTab.Screen.Send(end_line)
    objTab.Screen.WaitForString('#')
    log_message('erase_vlan_database: Found prompt after erase {}.'.format(variables['vlan_directory']))

    objTab.Screen.Send('delete {}vlan.dat{}'.format(variables['directory'], end_line))
    index = objTab.Screen.WaitForStrings(['[confirm]', '#', 'No such file'], 30)
    if index == 1:
        objTab.Screen.Send(end_line)
        objTab.Screen.WaitForString('#')
    elif index == 0:
        dump_screen_for_debug('erase_vlan_database: delete vlan.dat timed out waiting for a recognized prompt')
        objTab.Screen.Send(end_line)
        objTab.Screen.WaitForString('#')
    log_message('erase_vlan_database: Also attempted delete {}vlan.dat (index {})'
                .format(variables['directory'], index))


def write_erase():
    objTab.Screen.Send('write erase' + end_line)
    log_message('write_erase: Sent write erase command!')
    index = objTab.Screen.WaitForStrings(['[confirm]', 'Continue?', '#'], 30)
    if index in (1, 2):
        objTab.Screen.Send(end_line)
    objTab.Screen.WaitForString('#')
    log_message('write_erase: Found Prompt!')


def reload_device():
    """
    Send the reload command, explicitly answering 'no' to any prompt asking
    to save the running-config.
    """
    objTab.Screen.Send('reload' + end_line)
    index = objTab.Screen.WaitForStrings(['Save?', 'not been saved', 'confirm'], 45)
    if index == 1:
        objTab.Screen.Send('no' + end_line)
    elif index in (2, 3):
        objTab.Screen.Send(end_line)
    else:
        dump_screen_for_debug('reload_device: timed out waiting for a recognized prompt')
        objTab.Screen.Send(end_line)
    log_message('reload_device: Sent reload, confirmed (index {}).'.format(index))


def show_info():
    objTab.Screen.Send('show version' + end_line)
    objTab.Screen.WaitForString('#')
    objTab.Screen.Send('show vlan brief' + end_line)
    objTab.Screen.WaitForString('#')
    log_message('show_info: Displayed version/vlan for confirmation.')


# ====================================================================================================================
# Orchestration
# ====================================================================================================================

def handle_device():
    """
    Assumes the device has already been interrupted to the rommon prompt
    (Ctrl+C/Ctrl+Break during boot). Login credentials may be unknown, so
    this uses the config-register bypass instead of trying to authenticate.

    Fully automated end to end:
    - Boots past VSS's self-reload restriction (clears VS_SWITCH_NUMBER)
    - Deletes every file except the boot image
    - Clears the VLAN database
    - Erases startup-config
    - Restores the config register to default (0x2102)
    - Reloads
    """
    directory = variables['directory']
    bypass_startup_config()
    wait_for_unauthenticated_prompt()
    enable_no_password()
    time.sleep(3)
    disable_pagination()
    time.sleep(3)
    files = get_directory_contents(directory)
    clean_directory(directory, files)
    erase_vlan_database()
    write_erase()
    restore_config_register()
    show_info()
    reload_device()
    log_message('handle_device: Cleanup complete.')


main()
