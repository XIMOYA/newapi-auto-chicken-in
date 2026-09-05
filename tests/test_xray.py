"""
tests/test_xray.py
xray 管理器的纯逻辑测试（不起真进程、不联网）。

守的是「配置错了但看不出来」这类问题：
- 节点标识与本地入口必须是两个键，混用会让反馈全记到 127.0.0.1
- 不支持的节点要跳过并给原因，且原因里不许出现 uuid（会进日志）
- 端口不能重复、路由不能交叉，否则流量走错节点且完全无感
- 配置结构必须与 server/xray_config.go 一致，两边漂了会「平台测得通、客户端连不上」
"""

import json

import pytest

from newapi_checkin.xray import (
    Binding,
    LOCAL_HOST,
    VlessNode,
    XrayManager,
    build_config,
    node_label,
    node_supported,
    parse_vless_uri,
)

UUID1 = "11111111-1111-4000-8000-000000000000"
UUID2 = "22222222-2222-4000-8000-000000000000"


def reality_uri(host="a.example.com", uuid=UUID1, extra=""):
    return (
        f"vless://{uuid}@{host}:443?type=tcp&security=reality"
        f"&pbk=PUBKEY&sid=ab12&fp=chrome&sni=w.example.com{extra}#节点-{host}"
    )


class TestParseVlessUri:
    def test_full_node(self):
        node = parse_vless_uri(reality_uri())
        assert node is not None
        assert node.uuid == UUID1
        assert node.host == "a.example.com"
        assert node.port == 443
        assert node.tag == "节点-a.example.com"
        assert node.raw == reality_uri()  # 逐字保留：它是平台侧的主键
        assert node.security == "reality"
        assert node.network == "tcp"
        assert node.server_name == "w.example.com"

    def test_defaults(self):
        """省略 type/security 时按各家客户端的通行默认处理。"""
        node = parse_vless_uri(f"vless://{UUID1}@b.example.com:8443")
        assert node is not None
        assert node.network == "tcp"
        assert node.security == "none"
        # 没有 sni/host 时回落节点 host —— 纯 tls 节点常不写 sni
        assert node.server_name == "b.example.com"

    @pytest.mark.parametrize("uri", [
        "", "   ", None,
        "trojan://uuid@a.com:443",
        "1.2.3.4:8080",
        f"vless://@a.example.com:443",          # 缺 uuid
        f"vless://{UUID1}@a.example.com",        # 缺端口
        f"vless://{UUID1}@:443",                 # 缺主机
    ])
    def test_rejects_invalid(self, uri):
        assert parse_vless_uri(uri) is None

    def test_ipv6(self):
        node = parse_vless_uri(f"vless://{UUID1}@[2001:db8::1]:443?security=tls")
        assert node is not None and node.host == "2001:db8::1" and node.port == 443


class TestNodeSupported:
    def test_reality_ok(self):
        assert node_supported(parse_vless_uri(reality_uri())) == (True, "")

    def test_tls_ok(self):
        node = parse_vless_uri(f"vless://{UUID1}@a.example.com:443?security=tls")
        ok, why = node_supported(node)
        assert ok and why == ""

    def test_rejects_unsupported_security(self):
        node = parse_vless_uri(f"vless://{UUID1}@a.example.com:443?security=none")
        ok, why = node_supported(node)
        assert not ok and "security" in why

    def test_rejects_non_tcp(self):
        node = parse_vless_uri(f"vless://{UUID1}@a.example.com:443?type=ws&security=tls")
        ok, why = node_supported(node)
        assert not ok and "ws" in why

    def test_reality_needs_pbk(self):
        """缺 pbk 的 reality 节点握不了手，进配置只是白占一个端口。"""
        node = parse_vless_uri(f"vless://{UUID1}@a.example.com:443?security=reality")
        ok, why = node_supported(node)
        assert not ok and "pbk" in why


def test_node_label_never_leaks_uuid():
    """标签会进日志，绝不能带 uuid（那是接入凭据）。"""
    node = parse_vless_uri(reality_uri())
    label = node_label(node)
    assert UUID1 not in label
    assert "***" in label
    assert "a.example.com:443" in label  # 但要认得出是哪个节点


class TestBuildConfig:
    def test_structure_matches_go_side(self):
        """结构必须与 server/xray_config.go 一致，两边漂了会「平台测得通、客户端连不上」。"""
        node = parse_vless_uri(reality_uri(extra="&flow=xtls-rprx-vision"))
        cfg, bindings, skipped = build_config([node], [22801])
        assert skipped == []

        inbound = cfg["inbounds"][0]
        # 只听回环：听 0.0.0.0 等于把明文 socks 白送给同网段
        assert inbound == {"listen": LOCAL_HOST, "port": 22801,
                           "protocol": "socks", "tag": "in-0"}

        outbound = cfg["outbounds"][0]
        assert outbound["protocol"] == "vless"
        assert outbound["tag"] == "out-0"
        vnext = outbound["settings"]["vnext"][0]
        assert vnext["address"] == "a.example.com" and vnext["port"] == 443
        user = vnext["users"][0]
        assert user["id"] == UUID1
        # encryption 恒为 none，写别的值 xray 拒绝启动
        assert user["encryption"] == "none"
        assert user["flow"] == "xtls-rprx-vision"

        stream = outbound["streamSettings"]
        assert stream["network"] == "tcp" and stream["security"] == "reality"
        assert stream["realitySettings"]["publicKey"] == "PUBKEY"
        assert stream["realitySettings"]["shortId"] == "ab12"
        assert stream["realitySettings"]["serverName"] == "w.example.com"
        # 两个 settings 互斥
        assert "tlsSettings" not in stream

        # 路由一对一绑定：单进程多节点全靠它
        assert cfg["routing"]["rules"][0] == {
            "type": "field", "inboundTag": ["in-0"], "outboundTag": "out-0"}

        assert bindings[0] == Binding(
            node_addr=node.raw, local_proxy=f"socks5://{LOCAL_HOST}:22801",
            inbound_tag="in-0")

    def test_flow_omitted_when_absent(self):
        """节点没给 flow 就不带：填错的 flow 会直接握手失败，给默认值更危险。"""
        cfg, _, _ = build_config([parse_vless_uri(reality_uri())], [22801])
        assert "flow" not in cfg["outbounds"][0]["settings"]["vnext"][0]["users"][0]

    def test_tls_node_uses_tls_settings(self):
        node = parse_vless_uri(f"vless://{UUID1}@a.example.com:443?security=tls&alpn=h2,http/1.1")
        cfg, _, _ = build_config([node], [22801])
        stream = cfg["outbounds"][0]["streamSettings"]
        assert stream["tlsSettings"]["serverName"] == "a.example.com"
        assert stream["tlsSettings"]["alpn"] == ["h2", "http/1.1"]
        assert "realitySettings" not in stream

    def test_multiple_nodes_distinct_ports_and_tags(self):
        nodes = [parse_vless_uri(reality_uri(host="a.example.com")),
                 parse_vless_uri(reality_uri(host="b.example.com", uuid=UUID2))]
        cfg, bindings, skipped = build_config(nodes, [22801, 22802])
        assert skipped == [] and len(bindings) == 2
        ports = [i["port"] for i in cfg["inbounds"]]
        assert ports == [22801, 22802]
        # tag 撞了路由会指向错误的出站，流量走错节点且完全无感
        assert len({i["tag"] for i in cfg["inbounds"]}) == 2
        assert len({o["tag"] for o in cfg["outbounds"]}) == 2
        for i, rule in enumerate(cfg["routing"]["rules"]):
            assert rule["inboundTag"] == [cfg["inbounds"][i]["tag"]]
            assert rule["outboundTag"] == cfg["outbounds"][i]["tag"]
        # 两个键分开：反馈按原始 URI，连接按本地地址
        assert bindings[0].node_addr != bindings[0].local_proxy

    def test_skips_unsupported_with_reasons(self):
        nodes = [
            parse_vless_uri(f"vless://{UUID1}@ws.example.com:443?type=ws&security=tls#WS"),
            parse_vless_uri(f"vless://{UUID2}@none.example.com:443?security=none#N"),
            parse_vless_uri(reality_uri(host="ok.example.com")),
        ]
        cfg, bindings, skipped = build_config(nodes, [22801, 22802, 22803])
        assert len(bindings) == 1 and len(cfg["inbounds"]) == 1
        assert len(skipped) == 2
        joined = "\n".join(skipped)
        # 跳过原因会进日志
        assert UUID1 not in joined and UUID2 not in joined
        # 跳过的不占端口号
        assert bindings[0].local_proxy.endswith(":22801")

    def test_ports_exhausted(self):
        """端口不够时多出来的节点按跳过处理，而不是复用同一个端口。"""
        nodes = [parse_vless_uri(reality_uri(host=f"n{i}.example.com")) for i in range(3)]
        cfg, bindings, skipped = build_config(nodes, [22801])
        assert len(bindings) == 1
        assert len(skipped) == 2
        assert any("端口" in s for s in skipped)

    def test_empty_when_nothing_supported(self):
        """全不支持时给空 inbounds，调用方据此判定不用起 xray。"""
        node = parse_vless_uri(f"vless://{UUID1}@a.example.com:443?security=none")
        cfg, bindings, skipped = build_config([node], [22801])
        assert cfg["inbounds"] == [] and bindings == []
        assert len(skipped) == 1
        json.dumps(cfg)  # 空配置也要能正常序列化


class TestManagerWithoutProcess:
    def test_no_nodes_never_starts(self):
        """没有节点时不起进程，proxy_for 返回空串让调用方回落原有代理。"""
        mgr = XrayManager([])
        mgr.start()
        assert not mgr.running
        assert mgr.proxy_for("vless://whatever") == ""
        assert mgr.local_proxies() == []
        mgr.stop()  # 重复/空停止必须安全
        mgr.stop()

    def test_all_unsupported_never_starts(self):
        node = parse_vless_uri(f"vless://{UUID1}@a.example.com:443?security=none")
        mgr = XrayManager([node])
        mgr.start()
        assert not mgr.running
        assert len(mgr.skipped) == 1
        mgr.stop()

    def test_proxy_for_unknown_node(self):
        mgr = XrayManager([])
        mgr._by_node = {"vless://known": "socks5://127.0.0.1:1"}
        assert mgr.proxy_for("vless://known") == "socks5://127.0.0.1:1"
        assert mgr.proxy_for("vless://unknown") == ""
        assert mgr.proxy_for("") == ""
        assert mgr.proxy_for(None) == ""
