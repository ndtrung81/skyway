# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# Maintainer: Yuxing Peng, Trung Nguyen

from subprocess import PIPE, Popen

# execute a command, return output as a list of rows, each row is converted to a list of words
def proc(command, strict=True):
    if isinstance(command, list):
        command = ' '.join(command)
    p = Popen(command, shell=True, stdout=PIPE, stderr=PIPE)
    stdout, stderr = p.communicate()
    out = stdout.decode('ascii').strip()
    err = stderr.decode('utf-8').strip()
    
    if strict and err !="":
        raise Exception('Shell error: ' + err + '\nCommand: ' + command)
    
    if out == "": return []
    else: return out.split('\n')

# get the username of a uid
def get_username(uid):
    uid = proc("getent passwd " + uid + " | awk -F: '{print $1}'")
    return None if uid==[] else uid[0]

def sendmail(email_address, to, subject, body):
    mail = "\r\n".join([
        'From: ' + email_address,
        'To: ' + to,
        'Subject: ' + subject,
        '',
        body
    ])
    proc("echo \"" + mail + "\" | sendmail -v '" + to + "'")