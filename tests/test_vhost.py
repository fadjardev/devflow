from pathlib import Path

from devflow import vhost
from devflow.projects import Vhost

CONF = """# asm.qhomemart.test
server {
    listen       443 ssl;
    server_name  asm.qhomemart.test;

    root   projects/asm.qhomemart.cloud;
    ssl_certificate      certs/_wildcard.qhomemart.test.pem;
    access_log  logs/asm.qhomemart.test.access.log  main;
    error_log   logs/asm.qhomemart.test.error.log   warn;
    location ~ \\.php$ { fastcgi_pass    127.0.0.1:9082; }
}
"""


def test_temp_host_stays_under_wildcard():
    assert vhost.temp_host("asm.qhomemart.test", "261002-01") == "asm-261002-01.qhomemart.test"
    assert vhost.temp_host("ic-stokdigital.test", "261002-01") == "ic-stokdigital-261002-01.test"


def test_render_rewrites_name_root_logs_not_certs():
    vh = Vhost(Path("x.conf"), "asm.qhomemart.test", "asm.qhomemart.cloud", "", 9082)
    host, text = vhost.render(CONF, vh, "261002-01", "asm.qhomemart.cloud-261002-01")
    assert host == "asm-261002-01.qhomemart.test"
    assert "server_name  asm-261002-01.qhomemart.test;" in text
    assert "root   projects/.worktrees/asm.qhomemart.cloud-261002-01;" in text
    assert "logs/asm-261002-01.qhomemart.test.access.log" in text
    assert "certs/_wildcard.qhomemart.test.pem" in text
    assert "fastcgi_pass    127.0.0.1:9082" in text


def test_render_keeps_public_subdir():
    conf = "server {\n server_name qsales.qhomeapps.test;\n root projects/qsales.qhomeapps.test/public;\n}\n"
    vh = Vhost(Path("x.conf"), "qsales.qhomeapps.test", "qsales.qhomeapps.test", "public", 9082)
    _, text = vhost.render(conf, vh, "261002-02", "qsales.qhomeapps-261002-02")
    assert "root projects/.worktrees/qsales.qhomeapps-261002-02/public;" in text


def test_opencode_deny_patterns_match_absolute_paths():
    from devflow.opencode import path_pattern
    assert path_pattern(".env") == "*.env"
    assert path_pattern(".env.*") == "*.env.*"
    assert path_pattern("application/config/database.php") == "*application*config*database.php"
    assert path_pattern("**/*-key.pem") == "*-key.pem"
