"""Rule test for src/lemoncrow/pro/capabilities/review/security_rules/python.yaml.

Run it with `opengrep scan --test --config <rules dir> tests/fixtures/opengrep`. Every `ruleid:` line must be
reported by that rule and every `ok:` line must not be. Nothing here runs: it is sample text for the scanner.
"""

import hashlib
import json
import marshal
import os
import pickle
import shutil
import ssl
import subprocess

import requests
import yaml
from Crypto.Cipher import AES, DES  # type: ignore[import-not-found]
from flask import request, send_file
from werkzeug.utils import secure_filename


def shell_true(cmd, name):
    # ruleid: lemoncrow.python.subprocess-shell-true
    subprocess.run(cmd, shell=True)
    # ruleid: lemoncrow.python.subprocess-shell-true
    subprocess.Popen("echo " + name, shell=True)
    # ok: lemoncrow.python.subprocess-shell-true
    subprocess.run("ls -l", shell=True)
    # ok: lemoncrow.python.subprocess-shell-true
    subprocess.run(["ls", name])


def os_command(name):
    # ruleid: lemoncrow.python.os-command-dynamic
    os.system(f"ls {name}")
    # ruleid: lemoncrow.python.os-command-dynamic
    os.popen("cat " + name)
    # ok: lemoncrow.python.os-command-dynamic
    os.system("ls -l")


def dynamic_code(source):
    # ruleid: lemoncrow.python.eval-exec-dynamic
    eval(source)
    # ruleid: lemoncrow.python.eval-exec-dynamic
    exec(source)
    # ok: lemoncrow.python.eval-exec-dynamic
    eval("1 + 1")


def sql(cursor, user_id, name):
    # ruleid: lemoncrow.python.sql-string-building
    cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
    # ruleid: lemoncrow.python.sql-string-building
    cursor.execute("SELECT * FROM users WHERE name = '" + name + "'")
    # ruleid: lemoncrow.python.sql-string-building
    cursor.execute("SELECT * FROM users WHERE id = %s" % user_id)  # noqa: UP031
    # ruleid: lemoncrow.python.sql-string-building
    cursor.execute("SELECT * FROM users WHERE id = {}".format(user_id))  # noqa: UP032
    # ok: lemoncrow.python.sql-string-building
    cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    # ok: lemoncrow.python.sql-string-building
    cursor.execute("SELECT 1")


def deserialize(blob, handle):
    # ruleid: lemoncrow.python.pickle-deserialization
    pickle.loads(blob)
    # ruleid: lemoncrow.python.pickle-deserialization
    pickle.load(handle)
    # ruleid: lemoncrow.python.pickle-deserialization
    marshal.loads(blob)
    # ok: lemoncrow.python.pickle-deserialization
    json.loads(blob)
    # ruleid: lemoncrow.python.yaml-unsafe-load
    yaml.load(blob)
    # ruleid: lemoncrow.python.yaml-unsafe-load
    yaml.load(blob, Loader=yaml.Loader)
    # ruleid: lemoncrow.python.yaml-unsafe-load
    yaml.unsafe_load(blob)
    # ok: lemoncrow.python.yaml-unsafe-load
    yaml.safe_load(blob)
    # ok: lemoncrow.python.yaml-unsafe-load
    yaml.load(blob, Loader=yaml.SafeLoader)


def hashes(data):
    # ruleid: lemoncrow.python.weak-hash
    hashlib.md5(data)
    # ruleid: lemoncrow.python.weak-hash
    hashlib.sha1(data).hexdigest()
    # ok: lemoncrow.python.weak-hash
    hashlib.md5(data, usedforsecurity=False)
    # ok: lemoncrow.python.weak-hash
    hashlib.sha256(data)


def ciphers(key):
    # ruleid: lemoncrow.python.weak-cipher
    AES.new(key, AES.MODE_ECB)
    # ruleid: lemoncrow.python.weak-cipher
    DES.new(key, DES.MODE_CBC)
    # ok: lemoncrow.python.weak-cipher
    AES.new(key, AES.MODE_GCM)


def tls(url):
    # ruleid: lemoncrow.python.tls-verification-disabled
    requests.get(url, verify=False)
    # ruleid: lemoncrow.python.tls-verification-disabled
    ssl._create_unverified_context()
    # ok: lemoncrow.python.tls-verification-disabled
    requests.get(url, timeout=5)


def fetch_for_user():
    target = request.args.get("url")
    # ruleid: lemoncrow.python.ssrf-user-controlled-url
    requests.get(target, timeout=5)
    # ok: lemoncrow.python.ssrf-user-controlled-url
    requests.get("https://api.example.invalid/status", timeout=5)


def read_for_user():
    name = request.args.get("file")
    # ruleid: lemoncrow.python.path-traversal-user-input
    open(name)
    # ruleid: lemoncrow.python.path-traversal-user-input
    send_file(os.path.join("/srv/files", name))
    # ok: lemoncrow.python.path-traversal-user-input
    send_file(os.path.join("/srv/files", secure_filename(name)))
    # ok: lemoncrow.python.path-traversal-user-input
    shutil.rmtree("/tmp/fixed-directory")
