"""多租户：一套服务多个项目，各带各的护栏。

最要紧的一条：**成人产品的宽松绝不能漏到儿童产品**。
所以规则跟着租户走，而且配错了要在加载时就炸——不能等到线上有孩子在用。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from voice_kit.tenants import Registry, hash_key


KID = {
    "id": "qiuqiu", "name": "球球老师", "key_sha256": hash_key("kid-key"),
    "system_prompt": "你是球球老师。",
    "output_rules": [{"name": "禁用病理化词汇", "words": ["抑郁", "焦虑症"]},
                     {"name": "禁止主动结束", "patterns": ["今天.{0,4}(到这里|就聊到)"]}],
    "input_rules": [{"name": "self_harm", "words": ["不想活"]}],
    "fallback_reply": "我们再多聊一会儿吧。",
    "on_input_trigger_reply": "我听到了……",
    "voice_role": "qiuqiu",
}
ADULT = {"id": "coach", "name": "AI 教练", "key_sha256": hash_key("adult-key"),
         "system_prompt": "你是教练。", "voice_role": "coach"}


def write(tmp_path, tenants):
    p = tmp_path / "tenants.json"
    p.write_text(json.dumps({"tenants": tenants}, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------- 隔离

def test_each_project_has_its_own_key(tmp_path):
    r = Registry(write(tmp_path, [KID, ADULT]))
    assert r.by_key("kid-key").id == "qiuqiu"
    assert r.by_key("adult-key").id == "coach"
    assert r.by_key("wrong") is None
    assert r.by_key("") is None


def test_rules_do_not_leak_between_tenants(tmp_path):
    """**整个设计的核心。** 儿童产品的严格不该套在成人产品上，
    而成人产品的宽松绝不能漏到儿童产品。"""
    r = Registry(write(tmp_path, [KID, ADULT]))
    kid, adult = r.by_id("qiuqiu"), r.by_id("coach")
    assert [x.name for x in kid.output_rules] == ["禁用病理化词汇", "禁止主动结束"]
    assert adult.output_rules == []
    assert kid.input_rules and not adult.input_rules


def test_one_key_cannot_reach_another_tenants_config(tmp_path):
    r = Registry(write(tmp_path, [KID, ADULT]))
    t = r.by_key("adult-key")
    assert t.system_prompt == "你是教练。"
    assert "球球" not in t.system_prompt


# ---------------------------------------------------------------- 配错要早炸

def test_gate_without_fallback_fails_at_load(tmp_path):
    """有闸门没兜底 = 违规被拦下时用户看到一片空白。
    **必须在加载时炸**，不能等线上有孩子在用才发现。"""
    bad = dict(KID)
    bad["fallback_reply"] = ""
    with pytest.raises(ValueError, match="fallback_reply"):
        Registry(write(tmp_path, [bad]))


def test_input_rules_without_takeover_reply_fails(tmp_path):
    bad = dict(KID)
    bad["fallback_reply"] = ""
    bad["on_input_trigger_reply"] = ""
    with pytest.raises(ValueError):
        Registry(write(tmp_path, [bad]))


def test_bad_config_does_not_half_load(tmp_path):
    """一个租户配错，不能让另一个半生效——那样最难查。"""
    bad = dict(ADULT)
    bad["output_rules"] = [{"name": "x", "words": ["y"]}]     # 有规则没兜底
    with pytest.raises(ValueError):
        Registry(write(tmp_path, [KID, bad]))


# ---------------------------------------------------------------- 运维性

def test_hot_reload_picks_up_new_tenant(tmp_path):
    """加一个项目 = 改配置文件，**不用发版**。
    这是"公共能力"和"探针"的分界线之一。"""
    p = write(tmp_path, [KID])
    r = Registry(p)
    assert r.by_key("adult-key") is None
    p.write_text(json.dumps({"tenants": [KID, ADULT]}, ensure_ascii=False),
                 encoding="utf-8")
    import os
    os.utime(p, (0, 0))          # 强制 mtime 变化
    r.maybe_reload()
    assert r.by_key("adult-key").id == "coach"


def test_missing_file_is_empty_not_crash(tmp_path):
    """配置文件还没建好时，服务要能起来（空租户表），
    而不是起不来——否则第一次部署就卡住。"""
    r = Registry(tmp_path / "nope.json")
    assert r.all() == []


def test_keys_are_stored_hashed_only(tmp_path):
    """配置文件会进备份、会被 scp、会被人看一眼。
    存明文等于把所有项目的凭据放在随手可见的地方。"""
    p = write(tmp_path, [KID, ADULT])
    raw = p.read_text(encoding="utf-8")
    assert "kid-key" not in raw
    assert "adult-key" not in raw
    assert hash_key("kid-key") in raw


def test_regex_rules_are_compiled(tmp_path):
    r = Registry(write(tmp_path, [KID]))
    rule = [x for x in r.by_id("qiuqiu").output_rules if x.patterns][0]
    assert rule.patterns[0].search("今天就聊到这里吧")
    assert not rule.patterns[0].search("我们接着聊")


def test_thinking_defaults_off(tmp_path):
    """语音场景默认关思考——实测首字 3726→1387ms。
    默认值站在"用户在等着听声音"这一边。"""
    r = Registry(write(tmp_path, [ADULT]))
    assert r.by_id("coach").thinking is False
