# $language = "python"
# $interface = "1.0"
"""
cisco_n9k_reset.py
Andrew Boakye

Automates a full factory reset of a Cisco Nexus 9000 series switch
(tested against N9K-C9348GC-FXP running NX-OS) via SecureCRT console session.

WHY THIS IS A DIFFERENT SCRIPT AND NOT JUST A PORT
----------------------------------------------------
NX-OS is architecturally different from the IOS-XE ROMMON flow used on
Catalyst switches:

  * There is no "switch:" ROMMON prompt with a full file system shell.
    The Nexus boot-loader prompt ("loader>") only supports a handful of
    commands (dir, boot, cmdline, set, unset) -- it CANNOT delete files.
    So all file cleanup here happens from the regular NX-OS CLI
    ("bootflash:" filesystem), not from the loader.

  * "write erase" only clears startup-config (nvram). It does NOT touch
    bootflash: files, so logs/cores/etc. must be deleted separately.

  * There is no "enable" level separate from login -- once at a "#"
    prompt you are already at the privileged level.

  * The current NX-OS image is a single unified .bin (no separate
    kickstart image on this hardware/software train).

  * The switch's existing login credentials may be unknown, so this script
    uses Cisco's documented password-recovery boot ("cmdline
    recoverymode=1" at the loader) to reach a maintenance shell with NO
    login required.

  * NX-OS forces a MANDATORY admin password setup on the first normal
    boot after "write erase" + "reload". This script drives through that
    setup automatically (POAP abort, admin password from
    variables['new_password'], basic config dialog declined) so the whole
    run is hands-off.

IMPORTANT -- VALIDATE IN A LAB FIRST
----------------------------------------------------
Exact prompt wording differs across NX-OS releases. This script logs
everything to n9k_log.txt, including a full screen dump whenever a wait
times out, so check the log after each run and adjust as needed.

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
    log_message("Starting Nexus 9K Reset Process: " + str(datetime.now()))
    log_message('Log File Directory: ' + get_log_path())
    handle_device()
    log_message('Ending Nexus 9K Reset Process.')
    log_message("***********************************************************************************\n\n")


# ====================================================================================================================
# Config / logging
# ====================================================================================================================

def load_variable_file():
    script_directory = os.path.dirname(os.path.abspath(__file__))
    variables_path = os.path.join(script_directory, "n9k_variables.json")
    if not os.path.exists(variables_path):
        log_message('load_variable_file: No variable file detected.')
        default_data = {
            "directory": "bootflash:",
            "boot_image_candidates": [],
            "keep_patterns": [],
            "protected_names": [],
            "loader_prompt": "loader >",
            "login_prompt": "login:",
            "new_password": "YOUR_TEMP_PASSWORD_HERE",   # <-- Set this in n9k_variables.json before running
            "enforce_secure_password_standard": False,
            "port_count": 48   # <-- Set to match your switch's physical port count
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
    return os.path.join(script_directory, "n9k_log.txt")


# ====================================================================================================================
# Boot / login handling
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


def enter_recovery_mode():
    """
    Set the loader's recoverymode flag so the next boot drops straight to a
    maintenance shell ('(boot)#') with NO login required. This is Cisco's
    documented password-recovery path.
    """
    objTab.Screen.Send('cmdline recoverymode=1' + end_line)
    index = objTab.Screen.WaitForStrings([variables['loader_prompt']], 30)
    if index == 0:
        dump_screen_for_debug('enter_recovery_mode: timed out waiting for loader prompt')
        display_to_user('Sent "cmdline recoverymode=1" but never saw the loader prompt again. '
                        'Check n9k_log.txt.')
        raise Exception('Timed out after cmdline recoverymode=1.')
    log_message('enter_recovery_mode: Set recoverymode=1.')


def boot_from_loader():
    """
    Try each filename in variables['boot_image_candidates'], in order,
    until one actually boots. A wrong filename fails fast with "Boot failed /
    Error 9" and returns to the loader prompt.
    """
    candidates = variables.get('boot_image_candidates') or []
    if not candidates:
        display_to_user('n9k_variables.json has no "boot_image_candidates" set. '
                        'Add at least one .bin filename before running this script.')
        raise Exception('boot_image_candidates not configured.')

    for candidate in candidates:
        objTab.Screen.Send('boot bootflash:{}{}'.format(candidate, end_line))
        log_message('boot_from_loader: Trying "boot bootflash:{}"'.format(candidate))
        index = objTab.Screen.WaitForStrings(
            ['Boot failed', 'Unknown boot failure', variables['loader_prompt']], 30)
        if index == 0:
            log_message('boot_from_loader: "{}" appears to be booting (no failure signal).'.format(candidate))
            return
        log_message('boot_from_loader: "{}" failed to boot, trying next candidate.'.format(candidate))

    display_to_user('None of the configured boot_image_candidates could be booted. '
                    'Check n9k_log.txt and verify the actual filenames in bootflash:.')
    raise Exception('No candidate boot image succeeded.')


def enter_recovery_mode_then_pause():
    enter_recovery_mode()
    log_message('enter_recovery_mode_then_pause: Pausing before boot command...')
    time.sleep(10)


def wait_for_maintenance_prompt():
    """
    Wait for the recovery-mode boot to finish, landing on '(boot)#' with no
    login. Polled in short chunks so the log shows continuous progress.

    Watches for the loader prompt reappearing (FN-70390 hardware issue on
    N9K-C9348GC-FXP) and raises RecoveryBootReset so the caller can retry.
    """
    poll_seconds = 15
    total_timeout = 180
    elapsed = 0
    index = 0

    while elapsed < total_timeout:
        index = objTab.Screen.WaitForStrings(
            ['(boot)#', variables['login_prompt'], '#', variables['loader_prompt']], poll_seconds)
        elapsed += poll_seconds
        if index != 0:
            break
        log_message('wait_for_maintenance_prompt: still waiting, ~{}s elapsed...'.format(elapsed))
        dump_screen_for_debug('wait_for_maintenance_prompt: {}s poll'.format(elapsed))

    if index == 0:
        dump_screen_for_debug('wait_for_maintenance_prompt: timed out')
        raise Exception('Timed out waiting for maintenance prompt.')

    dump_screen_for_debug('wait_for_maintenance_prompt: reached prompt (index {})'.format(index))

    if index == 4:
        log_message('wait_for_maintenance_prompt: device reset back to loader prompt (FN-70390-style crash).')
        raise RecoveryBootReset('Device reset back to loader prompt before completing boot.')

    if index == 2:
        display_to_user('Recovery mode still asked for a login -- check n9k_log.txt and the console '
                        'before going further; credentials may be needed after all.')
        raise Exception('Recovery mode unexpectedly required login.')

    log_message('wait_for_maintenance_prompt: Reached prompt (index {}).'.format(index))


class RecoveryBootReset(Exception):
    """Raised when the device resets back to loader> mid-boot instead of completing (see FN-70390)."""
    pass


def boot_into_maintenance_mode(max_attempts=4):
    """
    Wrap the recovery boot sequence with retries for the intermittent
    FN-70390-style reset on the N9K-C9348GC-FXP.
    """
    for attempt in range(1, max_attempts + 1):
        log_message('boot_into_maintenance_mode: attempt {}/{}'.format(attempt, max_attempts))
        enter_recovery_mode_then_pause()
        boot_from_loader()
        try:
            wait_for_maintenance_prompt()
            log_message('boot_into_maintenance_mode: succeeded on attempt {}.'.format(attempt))
            return
        except RecoveryBootReset as e:
            log_message('boot_into_maintenance_mode: attempt {} reset ({}).'.format(attempt, str(e)))
            if attempt == max_attempts:
                display_to_user('The switch reset back to the loader prompt {} times in a row instead of '
                                'completing the boot (known N9K-C9348GC-FXP issue, Cisco Field Notice '
                                'FN-70390 -- may need an EPLD upgrade). Giving up. Check n9k_log.txt.'
                                .format(max_attempts))
                raise
            time.sleep(20)


# ====================================================================================================================
# Bootflash cleanup
# ====================================================================================================================

def run_command_capture(command, timeout=45, last_n_rows=150):
    objTab.Screen.Send(command + end_line)
    index = objTab.Screen.WaitForStrings(['#'], timeout)
    if index == 0:
        dump_screen_for_debug('run_command_capture: "{}" timed out'.format(command))
        return ''
    time.sleep(1)
    end_row = objTab.Screen.CurrentRow
    start_row = max(1, end_row - last_n_rows + 1)
    return read_screen_range(start_row, end_row)


def suppress_confirmations():
    objTab.Screen.Send('terminal dont-ask' + end_line)
    objTab.Screen.WaitForString('#')
    log_message('suppress_confirmations: Sent terminal dont-ask.')


def get_bootflash_contents(directory):
    text = run_command_capture('dir {}'.format(directory))
    log_message('get_bootflash_contents: dir output:\n' + text)
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
    log_message('get_bootflash_contents: Parsed files: {}'.format(files))
    return files


def clean_bootflash(directory, files):
    """
    Delete every entry in `files` using plain 'delete bootflash:<name>' syntax.
    The device refuses to delete whatever image it's currently running --
    that's treated as an expected, successful skip.
    """
    keep_patterns = variables['keep_patterns']
    protected_names = variables['protected_names']

    for name in files:
        if any(pattern in name for pattern in keep_patterns):
            log_message('clean_bootflash: Skipping (matches keep_patterns) {}'.format(name))
            continue
        if name in protected_names:
            log_message('clean_bootflash: Skipping protected entry {}'.format(name))
            continue

        target = '{}{}'.format(directory, name)
        log_message('clean_bootflash: Deleting {}'.format(target))
        objTab.Screen.Send('delete {}{}'.format(target, end_line))

        reached_prompt = False
        for _ in range(4):
            index = objTab.Screen.WaitForStrings([
                'Do you want to continue',
                'Do you want to delete',
                'No such file',
                'Permission denied',
                'in use',
                'not allowed',
                '#'
            ], 60)

            if index in (1, 2):
                objTab.Screen.Send('y' + end_line)
                continue
            if index in (5, 6):
                log_message('clean_bootflash: {} is protected/in use by the live OS, leaving it.'.format(target))
            if index == 0:
                dump_screen_for_debug('clean_bootflash: unrecognized prompt deleting {}'.format(target))
                objTab.Screen.Send('n' + end_line)
                objTab.Screen.WaitForStrings(['#'], 30)

            reached_prompt = True
            break

        if not reached_prompt:
            log_message('clean_bootflash: WARNING - never confirmed {} finished deleting.'.format(target))
        log_message('clean_bootflash: Done with {}'.format(target))


def write_erase():
    objTab.Screen.Send('write erase' + end_line)
    log_message('write_erase: Sent write erase command!')
    index = objTab.Screen.WaitForStrings(['(y/n)', 'Continue?', '#'], 30)
    if index in (1, 2):
        objTab.Screen.Send('y' + end_line)
    objTab.Screen.WaitForString('#')
    log_message('write_erase: Found Prompt!')


def clear_logging():
    for command in ['clear logging logfile', 'clear logging onboard', 'clear accounting log']:
        objTab.Screen.Send(command + end_line)
        index = objTab.Screen.WaitForStrings(['#', 'Invalid command', '(y/n)'], 30)
        if index == 3:
            objTab.Screen.Send('y' + end_line)
            objTab.Screen.WaitForString('#')
        log_message('clear_logging: Ran "{}" (result index {})'.format(command, index))


def clear_cores():
    objTab.Screen.Send('clear cores' + end_line)
    index = objTab.Screen.WaitForStrings(['#', 'Invalid command'], 30)
    log_message('clear_cores: Result index {}'.format(index))


def remove_vlans():
    objTab.Screen.Send('conf t' + end_line)
    objTab.Screen.WaitForString('#')
    objTab.Screen.Send('no vlan 2-3967' + end_line)
    objTab.Screen.WaitForString('#')
    objTab.Screen.Send('end' + end_line)
    objTab.Screen.WaitForString('#')
    log_message('remove_vlans: Done.')


def enable_all_ports(port_count):
    """
    Enable every physical port (Ethernet1/1 through Ethernet1/<port_count>).
    'no shutdown' is idempotent -- harmless even for already-up ports.
    """
    objTab.Screen.Send('conf t' + end_line)
    objTab.Screen.WaitForString('#')
    for port in range(1, port_count + 1):
        objTab.Screen.Send('interface Ethernet1/{}{}'.format(port, end_line))
        objTab.Screen.WaitForString('#')
        objTab.Screen.Send('no shutdown' + end_line)
        objTab.Screen.WaitForString('#')
    objTab.Screen.Send('end' + end_line)
    objTab.Screen.WaitForString('#')
    log_message('enable_all_ports: Enabled ports 1-{}.'.format(port_count))


def reload_device():
    objTab.Screen.Send('reload' + end_line)
    index = objTab.Screen.WaitForStrings(['not been saved', '(y/n)'], 30)
    if index in (1, 2):
        objTab.Screen.Send('y' + end_line)
    log_message('reload_device: Sent reload, confirmed.')


def handle_post_reload_setup():
    """
    Fully automate the mandatory first-boot setup that follows 'write
    erase' + 'reload': POAP abort, secure-password-standard prompt,
    admin password + confirmation, and the basic configuration dialog.
    """
    seen_password_prompt = False
    for attempt in range(15):
        timeout = 600 if attempt == 0 else 60
        index = objTab.Screen.WaitForStrings([
            'Abort Power On Auto Provisioning',
            'Disable POAP',
            'enforce secure password standard',
            'Enter the password for',
            'Confirm the password for',
            'basic configuration dialog',
            variables['login_prompt'],
            '#',
            'too short',
            'does not meet',
            'weak password',
        ], timeout)

        if index in (1, 2):
            objTab.Screen.Send('yes' + end_line)
            log_message('handle_post_reload_setup: Answered yes to POAP prompt.')
        elif index == 3:
            answer = 'yes' if variables['enforce_secure_password_standard'] else 'no'
            objTab.Screen.Send(answer + end_line)
            log_message('handle_post_reload_setup: Secure password standard -> {}'.format(answer))
        elif index == 4:
            objTab.Screen.Send(variables['new_password'] + end_line)
            seen_password_prompt = True
            log_message('handle_post_reload_setup: Sent new admin password.')
        elif index == 5:
            objTab.Screen.Send(variables['new_password'] + end_line)
            log_message('handle_post_reload_setup: Confirmed new admin password.')
        elif index in (9, 10, 11):
            dump_screen_for_debug('handle_post_reload_setup: password rejected')
            display_to_user('NX-OS rejected the configured admin password (too short/weak). '
                            'Update "new_password" in n9k_variables.json and re-run.')
            raise Exception('Admin password rejected by NX-OS.')
        elif index == 6:
            objTab.Screen.Send('no' + end_line)
            log_message('handle_post_reload_setup: Declined basic configuration dialog.')
        elif index == 7:
            log_message('handle_post_reload_setup: Reached login prompt, logging in.')
            login_with_new_password()
            return
        elif index == 8:
            log_message('handle_post_reload_setup: Reached # prompt directly.')
            return
        else:
            dump_screen_for_debug('handle_post_reload_setup: timed out')
            log_message('handle_post_reload_setup: WaitForStrings timed out, stopping.')
            return

    if not seen_password_prompt:
        log_message('handle_post_reload_setup: WARNING - never saw the admin password prompt.')


def login_with_new_password():
    objTab.Screen.Send('admin' + end_line)
    index = objTab.Screen.WaitForStrings(['Password:', '#'], 30)
    if index == 1:
        objTab.Screen.Send(variables['new_password'] + end_line)
        objTab.Screen.WaitForString('#')
    log_message('login_with_new_password: Logged in.')


def show_info():
    objTab.Screen.Send('show inventory' + end_line)
    objTab.Screen.WaitForString('#')
    objTab.Screen.Send('show version' + end_line)
    objTab.Screen.WaitForString('#')
    objTab.Screen.Send('show vlan brief' + end_line)
    objTab.Screen.WaitForString('#')
    log_message('show_info: Displayed inventory/version/vlan for confirmation.')


# ====================================================================================================================
# Orchestration
# ====================================================================================================================

def handle_device():
    """
    Assumes the device has already been interrupted to the loader> prompt
    (Ctrl+C spammed during boot). Uses recovery-mode boot (no login required)
    since existing credentials may be unknown.

    Fully automated end to end, including the mandatory post-erase setup
    wizard NX-OS forces after 'write erase' + 'reload'.
    """
    directory = variables['directory']
    boot_into_maintenance_mode()
    write_erase()
    reload_device()
    handle_post_reload_setup()
    suppress_confirmations()
    files = get_bootflash_contents(directory)
    clean_bootflash(directory, files)
    clear_logging()
    clear_cores()
    remove_vlans()
    enable_all_ports(variables['port_count'])
    show_info()
    log_message('handle_device: Cleanup complete.')


main()
