"""Native order path: brain authority, genuine transport, evidence gates.

Covers issue #12: the trained UnifiedBrain owns main-path direction
(including the loss-pattern HOLD veto), review/storage use account-defined
risk, the Deriv universe is 13 symbols, the manager socket proxies to the
native EA file handoff (never fabricates ACKs), and dispatch requires
complete native evidence (BLOCKED_INCOMPLETE_EXPOSURE otherwise).

The vendored APEX bridge is faked (the vendor source is absent on this
checkout); every other component under test is the real implementation.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from abutron.config import (DATA_ROOT, MT5_ROOT,
                            DEFAULT_PER_TRADE_RISK_PCT,
                            DEFAULT_PORTFOLIO_RISK_CAP_PCT)
from abutron import db as dbmod
from abutron.events import EventLog
from abutron.apex_integration import (ApexIntegration, DERIV_UNIVERSE,
                                      build_instrument_manifest,
                                      _asset_class_for)
from abutron.brain.brain import BrainProposal
from abutron.crypto import hmac_sign, pin_binary
from abutron.execution.dispatcher import NativeDispatcher
from abutron.execution.ipc import ManagerRequest, ManagerSocketServer
from abutron.execution.native_evidence import (
    NATIVE_EVIDENCE_CATEGORIES, REASON_BLOCKED_INCOMPLETE_EXPOSURE,
    assess_native_evidence)
from abutron.execution.receipts import ReceiptManager
from abutron.execution.terminal_handoff import TerminalOrderProxy
from abutron.execution.writer_gateway import SingleWriterGateway
import abutron.execution.writer_gateway as gwmod
from abutron.feeds.feed_quality import Candle
from abutron.feeds.mt5_terminal import terminal_export_dir


SYM = 'Volatility 100 Index.0'


# ---------------------------------------------------------------------------
# Fake vendored APEX
# ---------------------------------------------------------------------------
class _FakeRejected(Exception):
    pass


class _FakeGate:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeNative:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeBridge:
    decision = {'status': 'BUY'}

    def __init__(self, instruments=None, config_path='', warmup_bars=0):
        self.instruments = instruments or []
        if not self.instruments:
            raise ValueError('no approved instruments for account')
        self.calls = []

    def on_closed_candle(self, raw, observed_at=None):
        self.calls.append(raw)
        return dict(_FakeBridge.decision)


def _fake_review_factory(captured, *, gates_pass=True, blockers=(),
                         signal_id=None):
    def _review(*, signal, gate, native, now, per_trade_risk_pct,
                portfolio_ceiling_pct):
        captured['signal_status'] = signal.get('status')
        captured['signal'] = dict(signal)
        captured['per_trade_risk_pct'] = per_trade_risk_pct
        captured['portfolio_ceiling_pct'] = portfolio_ceiling_pct
        captured['gate'] = gate
        if not gates_pass:
            return {'engineering_gates_passed': False,
                    'blockers': list(blockers) or ['G_FAIL'], 'review': {}}
        return {'engineering_gates_passed': True, 'blockers': [],
                'risk_budget_usd': 50.0, 'nominal_stop_risk_usd': 12.0,
                'order_candidate': {
                    'signal_id': signal_id or ('sig' + uuid.uuid4().hex),
                    'side': signal.get('status'), 'broker': 'deriv',
                    'symbol': signal.get('symbol', SYM),
                    'volume_lots': 1.0, 'stop_price': 99.5,
                    'take_price': 101.0}}
    return _review


def _install_fake_vendor(ai, decision, captured, **review_kw):
    _FakeBridge.decision = decision
    ai._apex_cache = {'bridge': _FakeBridge, 'CandleRejected': _FakeRejected,
                      'Instrument': object,
                      'review_candidate': _fake_review_factory(
                          captured, **review_kw),
                      'AccountGate': _FakeGate,
                      'BrokerNativeRisk': _FakeNative}


def _decision(status, symbol=SYM):
    return {'status': status, 'symbol': symbol, 'reason': 'fake',
            'regime': {'regime': 'trend'}, 'decision_id': 'd1',
            'conviction': 0.6, 'target_weight_research_only': 1.0,
            'signals': [{'agent': 'trend', 'score': 0.8}]}


# ---------------------------------------------------------------------------
# Brain stub (deterministic direction; real BrainProposal shape)
# ---------------------------------------------------------------------------
class _StubBrain:
    def __init__(self, direction='BUY', *, trained=True, blockers=(),
                 rationale='', boom=None):
        self._direction = direction
        self.trained = trained
        self._blockers = list(blockers)
        self._rationale = rationale
        self._boom = boom
        self.calls = []

    def decide(self, **kw):
        self.calls.append(kw)
        if self._boom is not None:
            raise self._boom
        return BrainProposal(symbol=kw.get('symbol', '?'),
                             direction=self._direction,
                             conviction=0.6, confidence=0.7,
                             uncertainty=0.2, regime=kw.get('regime', ''),
                             blockers=list(self._blockers),
                             rationale=self._rationale, trained=True,
                             features=[0.01] * 49)

    def online_learn_features(self, *a, **k):
        self.calls.append(('online', a, k))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def ai():
    log = EventLog(os.path.join(DATA_ROOT, 't_native_orders_events.jsonl'))
    return ApexIntegration(log, warmup_bars=0)


def _deriv_acct(**kw):
    d = {'connection_id': 'nev_conn', 'broker': 'deriv', 'mt5_login': 6207,
         'owner_id': 'nev_owner', 'desired_on': 1, 'effective_on': 1,
         'environment': 'demo', 'per_trade_risk_pct': 0.15,
         'portfolio_cap_pct': 0.20, 'symbols': SYM, 'equity': 10000.0}
    d.update(kw)
    return d


def _candle_dict(sym=SYM, i=0, base=100.0):
    t = 1781000000 + i * 300
    o = base + i * 0.1
    c = o + 0.05
    return {'symbol': sym, 'close_time_epoch': float(t),
            'open_time': '2026-11-16T08:00:00Z',
            'close_time': '2026-11-16T08:05:00Z',
            'open': o, 'high': c + 0.05, 'low': o - 0.05, 'close': c,
            'volume': 10.0}


def _prefill(ai, sym=SYM, n=20, base=100.0):
    ai._signal_hist[sym] = [
        Candle(symbol=sym, timeframe='M5', open_time='t', close_time='t',
               open=base + i, high=base + i + 1, low=base + i - 1,
               close=base + i + 0.5, volume=10.0)
        for i in range(n)]


_FULL_GATE = dict(account_bound=True, worker_nonce_bound=True,
                  fresh_broker_session=True, native_ea_account_match=True,
                  broker_trade_allowed=True, terminal_trade_allowed=True,
                  crm_entitlement_approved=True,
                  single_writer_exclusive=True,
                  broker_positions_protected=True,
                  broker_exposure_reconciled=True,
                  account_equity_usd=10000.0,
                  current_verified_stop_risk_usd=0.0)


def _geom(close):
    return {'tick_size': 0.01, 'tick_value_per_lot_usd': 1.0,
            'min_lot': 1.0, 'max_lot': 500.0, 'lot_step': 1.0,
            'min_stop_ticks': 1, 'broker_verified': True, 'price': close,
            'stop_price': close - 0.05, 'take_price': close + 0.2,
            'slippage_budget_ticks': 1, 'broker_calc_profit_verified': True,
            'broker_order_check_verified': True}


def _process(ai, acct, candle, captured, **kw):
    manifest = build_instrument_manifest([acct])
    return ai.process_candle(connection_id=acct['connection_id'],
                             account=acct, candle=candle, manifest=manifest,
                             equity=10000.0,
                             native_geometry=_geom(candle['close']),
                             gate_overrides=dict(_FULL_GATE), **kw)


def _uid(prefix):
    return '%s_%s' % (prefix, uuid.uuid4().hex[:10])


# ---------------------------------------------------------------------------
# 1) Brain authority over main-path direction
# ---------------------------------------------------------------------------
class TestBrainAuthority:
    def test_brain_buy_with_bridge_buy_is_ready(self, ai):
        acct = _deriv_acct()
        _prefill(ai)
        captured = {}
        _install_fake_vendor(ai, _decision('BUY'), captured)
        ai.brain = _StubBrain('BUY')
        out = _process(ai, acct, _candle_dict(), captured)
        assert out['status'] == 'READY'
        assert out['candidate']['side'] == 'BUY'
        assert captured['signal_status'] == 'BUY'
        dec = out['decision']
        assert dec['bridge_status'] == 'BUY'
        assert dec['brain_direction'] == 'BUY'
        assert dec['nexus_brain']['direction'] == 'BUY'
        assert len(dec['nexus_brain']['features']) == 49
        # the brain saw the full causal feature context
        assert ai.brain.calls and len(ai.brain.calls[0]['closes']) >= 10

    def test_loss_pattern_hold_vetoes_bridge_buy(self, ai):
        acct = _deriv_acct()
        _prefill(ai)
        captured = {}
        _install_fake_vendor(ai, _decision('BUY'), captured)
        ai.brain = _StubBrain(
            'HOLD', blockers=['LOSS_PATTERN_REPEAT:3_losses'],
            rationale='LOSS_PATTERN_REPEAT: unified brain vetoed')
        out = _process(ai, acct, _candle_dict(), captured)
        assert out['status'] == 'BRAIN_HOLD'
        assert out['candidate'] is None
        assert 'LOSS_PATTERN_REPEAT' in out['reason']
        assert out['decision']['bridge_status'] == 'BUY'
        assert out['decision']['brain_direction'] == 'HOLD'
        assert 'signal_status' not in captured  # review never ran

    def test_brain_sell_overrides_bridge_hold(self, ai):
        acct = _deriv_acct()
        _prefill(ai)
        captured = {}
        _install_fake_vendor(ai, _decision('HOLD'), captured)
        ai.brain = _StubBrain('SELL')
        out = _process(ai, acct, _candle_dict(), captured)
        assert out['status'] == 'READY'
        assert out['candidate']['side'] == 'SELL'
        assert captured['signal_status'] == 'SELL'
        assert out['decision']['direction_divergence'] == \
            'bridge=HOLD brain=SELL brain_wins'

    def test_brain_buy_overrides_bridge_sell(self, ai):
        acct = _deriv_acct()
        _prefill(ai)
        captured = {}
        _install_fake_vendor(ai, _decision('SELL'), captured)
        ai.brain = _StubBrain('BUY')
        out = _process(ai, acct, _candle_dict(), captured)
        assert out['status'] == 'READY'
        assert out['candidate']['side'] == 'BUY'
        assert 'brain_wins' in out['decision']['direction_divergence']

    def test_untrained_brain_blocks(self, ai):
        acct = _deriv_acct()
        _prefill(ai)
        captured = {}
        _install_fake_vendor(ai, _decision('BUY'), captured)
        ai.brain = _StubBrain('BUY', trained=False)
        out = _process(ai, acct, _candle_dict(), captured)
        assert out['status'] == 'BRAIN_UNTRAINED'
        assert out['candidate'] is None

    def test_brain_error_blocks(self, ai):
        acct = _deriv_acct()
        _prefill(ai)
        captured = {}
        _install_fake_vendor(ai, _decision('BUY'), captured)
        ai.brain = _StubBrain('BUY', boom=RuntimeError('nope'))
        out = _process(ai, acct, _candle_dict(), captured)
        assert out['status'] == 'BRAIN_ERROR'
        assert out['candidate'] is None

    def test_brain_warmup_blocks_fresh_symbol(self, ai):
        acct = _deriv_acct()
        captured = {}
        _install_fake_vendor(ai, _decision('BUY'), captured)
        ai.brain = _StubBrain('BUY')
        out = _process(ai, acct, _candle_dict(), captured)
        assert out['status'] == 'BRAIN_WARMUP'
        assert out['candidate'] is None


# ---------------------------------------------------------------------------
# 2) Account-defined risk + stored candidate propagation
# ---------------------------------------------------------------------------
class TestAccountRisk:
    def test_review_uses_account_risk(self, ai):
        acct = _deriv_acct(per_trade_risk_pct=0.15, portfolio_cap_pct=0.20)
        _prefill(ai)
        captured = {}
        _install_fake_vendor(ai, _decision('BUY'), captured)
        ai.brain = _StubBrain('BUY')
        out = _process(ai, acct, _candle_dict(), captured)
        assert out['status'] == 'READY'
        assert captured['per_trade_risk_pct'] == 0.15
        assert captured['portfolio_ceiling_pct'] == 0.20

    def test_review_risk_falls_back_to_deployment_defaults(self, ai):
        acct = _deriv_acct()
        del acct['per_trade_risk_pct']
        del acct['portfolio_cap_pct']
        _prefill(ai)
        captured = {}
        _install_fake_vendor(ai, _decision('BUY'), captured)
        ai.brain = _StubBrain('BUY')
        out = _process(ai, acct, _candle_dict(), captured)
        assert out['status'] == 'READY'
        assert captured['per_trade_risk_pct'] == DEFAULT_PER_TRADE_RISK_PCT
        assert captured['portfolio_ceiling_pct'] == \
            DEFAULT_PORTFOLIO_RISK_CAP_PCT

    def test_stored_candidate_carries_risk_entry_trail_json(self, ai):
        acct = _deriv_acct()
        _prefill(ai, n=30)
        captured = {}
        sig = 'sig' + uuid.uuid4().hex
        _install_fake_vendor(ai, _decision('BUY'), captured, signal_id=sig)
        ai.brain = _StubBrain('BUY')
        candle = _candle_dict()
        out = _process(ai, acct, candle, captured)
        assert out['status'] == 'READY'
        row = ai.store_ready_candidate(connection_id=acct['connection_id'],
                                       account=acct, ready=out, candle=candle)
        assert row is not None
        stored = dbmod.engine_db.get_candidate(row['candidate_id'])
        assert stored['risk_fraction'] == 0.15
        votes = json.loads(stored['agent_votes'])
        assert votes['brain_direction'] == 'BUY'
        assert len(votes['nexus_brain']['features']) == 49
        assert stored['entry_type'] == 'CONFIRM'
        assert stored['trigger_price'] == candle['close']
        assert stored['trail_k'] > 0
        assert stored['trail_stop'] != 0.0
        assert json.loads(stored['trail_plan'])['trail_stop'] != 0.0


# ---------------------------------------------------------------------------
# 3) Universe + asset classes
# ---------------------------------------------------------------------------
class TestUniverse:
    def test_deriv_universe_has_13(self):
        assert len(DERIV_UNIVERSE) == 13
        assert 'Volatility 100 Index.0' in DERIV_UNIVERSE
        assert 'Boom 1000 Index.0' in DERIV_UNIVERSE
        assert 'Crash 150 Index.0' in DERIV_UNIVERSE

    def test_symbol_less_deriv_manifest_covers_universe(self):
        manifest = build_instrument_manifest([{'broker': 'deriv'}])
        assert len(manifest) == 13
        assert {m['asset_class'] for m in manifest} == {'deriv_synthetic'}

    def test_fp_asset_classes(self):
        assert _asset_class_for('fpmarkets', 'GBPUSD') == 'fp_fx'
        assert _asset_class_for('fpmarkets', 'EURUSD.r') == 'fp_fx'
        assert _asset_class_for('fpmarkets', 'XAUUSD.r') == 'fp_index'
        assert _asset_class_for('fpmarkets', 'US30') == 'fp_index'
        assert _asset_class_for('deriv', 'Boom 1000 Index.0') == \
            'deriv_synthetic'


# ---------------------------------------------------------------------------
# Helpers: terminal drop zone staging
# ---------------------------------------------------------------------------
def _stage_identity(cid, login, root):
    d = terminal_export_dir(cid, root=root)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, 'ABUTRON_NATIVE_ACCOUNT_V2_%s.txt'
                        % uuid.uuid4().hex[:8])
    with open(path, 'w') as fh:
        json.dump({'connection_id': cid, 'login': login, 'nonce': 'n'}, fh)
    return d


def _write_receipt(export_dir, **kw):
    body = {'request_id': 'r', 'ticket': 0, 'state': 'ACK',
            'reason': 'NATIVE_ACK', 'login': 0, 'price': 1.0,
            'volume': 0.1, 'sl': 0.9, 'tp': 1.1, 'deal': '0',
            'ts': '2026-11-16T00:00:00Z'}
    body.update(kw)
    path = os.path.join(export_dir, 'ABUTRON_NATIVE_RECEIPTS_V2_%s.txt'
                        % uuid.uuid4().hex[:8])
    with open(path, 'w') as fh:
        json.dump(body, fh)
    return path


def _seed_account(cid, login, owner, **kw):
    row = {'connection_id': cid, 'broker': 'deriv', 'mt5_login': login,
           'owner_id': owner, 'display_name': cid, 'environment': 'demo',
           'server': 'Deriv-Demo', 'per_trade_risk_pct': 0.15,
           'portfolio_cap_pct': 0.20, 'desired_on': 1, 'effective_on': 1,
           'activation_revision': '', 'ea_sha256': '', 'symbols': SYM}
    row.update(kw)
    dbmod.engine_db.upsert_account(row)
    return dbmod.engine_db.get_account(cid)


def _order_request(cid, login, req_id='req1', rev=''):
    return {'kind': 'order', 'request_id': req_id, 'candidate_id': 'c1',
            'connection_id': cid, 'mt5_login': login, 'symbol': SYM,
            'direction': 'BUY', 'volume': 1.0, 'entry_type': 'CONFIRM',
            'trigger_price': 100.0, 'stop_loss': 99.5, 'take_profit': 101.0,
            'activation_revision': rev}


# ---------------------------------------------------------------------------
# 4) Genuine native transport (EA file handoff proxy)
# ---------------------------------------------------------------------------
class TestTerminalHandoff:
    def test_no_terminal_never_acks(self, tmp_path):
        cid, owner = _uid('ho1'), _uid('own1')
        _seed_account(cid, 6207, owner)
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path))
        resp = proxy.submit(_order_request(cid, 6207), timeout=2.0)
        assert resp['status'] == 'unknown'
        assert resp['reason'] == 'TERMINAL_EXPORT_MISSING'
        assert 'ticket' not in resp

    def test_genuine_receipt_returns_broker_ack(self, tmp_path):
        cid, owner = _uid('ho2'), _uid('own2')
        _seed_account(cid, 6207, owner)
        export_dir = _stage_identity(cid, 6207, str(tmp_path))
        _write_receipt(export_dir, request_id='req2', ticket=98765,
                       deal='123456', state='ACK', login=6207)
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path))
        resp = proxy.submit(_order_request(cid, 6207, req_id='req2'),
                            timeout=5.0)
        assert resp['status'] == 'ack'
        assert resp['ticket'] == 98765
        assert resp['deal'] == '123456'
        assert resp['request_id'] == 'req2'
        # the matched receipt is consumed; the EA-owned pending file stays
        left = [f for f in os.listdir(export_dir)
                if f.startswith('ABUTRON_NATIVE_RECEIPTS_V2_')
                and f.endswith('.txt')]
        assert left == []
        assert os.path.exists(os.path.join(export_dir,
                                           'ABUTRON_REQ_PENDING.json'))

    def test_foreign_login_receipt_not_accepted(self, tmp_path):
        cid, owner = _uid('ho3'), _uid('own3')
        _seed_account(cid, 6207, owner)
        export_dir = _stage_identity(cid, 6207, str(tmp_path))
        _write_receipt(export_dir, request_id='req3', ticket=111,
                       deal='222', state='ACK', login=999)
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path),
                                   poll_interval=0.05)
        resp = proxy.submit(_order_request(cid, 6207, req_id='req3'),
                            timeout=0.6)
        assert resp['status'] == 'unknown'
        assert 'ticket' not in resp

    def test_weak_ack_not_accepted(self, tmp_path):
        cid, owner = _uid('ho4'), _uid('own4')
        _seed_account(cid, 6207, owner)
        export_dir = _stage_identity(cid, 6207, str(tmp_path))
        _write_receipt(export_dir, request_id='req4', ticket=0, deal='0',
                       state='ACK', login=6207)
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path),
                                   poll_interval=0.05)
        resp = proxy.submit(_order_request(cid, 6207, req_id='req4'),
                            timeout=0.6)
        assert resp['status'] == 'unknown'

    def test_ea_reject_maps_to_reject(self, tmp_path):
        cid, owner = _uid('ho5'), _uid('own5')
        _seed_account(cid, 6207, owner)
        export_dir = _stage_identity(cid, 6207, str(tmp_path))
        _write_receipt(export_dir, request_id='req5', state='REJECTED',
                       reason='EA_BUSY', login=6207)
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path))
        resp = proxy.submit(_order_request(cid, 6207, req_id='req5'),
                            timeout=5.0)
        assert resp['status'] == 'reject'
        assert resp['reason'] == 'EA_BUSY'

    def test_writer_busy_when_pending_exists(self, tmp_path):
        cid, owner = _uid('ho6'), _uid('own6')
        _seed_account(cid, 6207, owner)
        export_dir = _stage_identity(cid, 6207, str(tmp_path))
        with open(os.path.join(export_dir, 'ABUTRON_REQ_PENDING.json'),
                  'w') as fh:
            fh.write('{"request_id": "other"}')
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path))
        resp = proxy.submit(_order_request(cid, 6207, req_id='req6'),
                            timeout=2.0)
        assert resp['status'] == 'reject'
        assert resp['reason'] == 'WRITER_BUSY'

    def test_request_login_mismatch_rejected(self, tmp_path):
        cid, owner = _uid('ho7'), _uid('own7')
        _seed_account(cid, 6207, owner)
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path))
        resp = proxy.submit(_order_request(cid, 9999), timeout=2.0)
        assert resp['status'] == 'reject'
        assert resp['reason'] == 'LOGIN_MISMATCH'

    def test_activation_mismatch_rejected(self, tmp_path):
        cid, owner = _uid('ho8'), _uid('own8')
        _seed_account(cid, 6207, owner, activation_revision='pinned-rev')
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path))
        resp = proxy.submit(_order_request(cid, 6207, rev='wrong'),
                            timeout=2.0)
        assert resp['status'] == 'reject'
        assert resp['reason'] == 'ACTIVATION_REVISION_MISMATCH'

    def test_receipt_timeout_leaves_pending_for_ea(self, tmp_path):
        cid, owner = _uid('ho9'), _uid('own9')
        _seed_account(cid, 6207, owner)
        export_dir = _stage_identity(cid, 6207, str(tmp_path))
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path),
                                   poll_interval=0.05)
        resp = proxy.submit(_order_request(cid, 6207, req_id='req9'),
                            timeout=0.5)
        assert resp['status'] == 'unknown'
        assert resp['reason'] == 'EA_RECEIPT_TIMEOUT'
        assert os.path.exists(os.path.join(export_dir,
                                           'ABUTRON_REQ_PENDING.json'))

    def test_manager_socket_verifies_hmac(self, tmp_path):
        cid, owner = _uid('ho10'), _uid('own10')
        _seed_account(cid, 6207, owner)
        proxy = TerminalOrderProxy(None, mt5_root=str(tmp_path))
        reached = []
        srv = ManagerSocketServer(secret='s3cret')

        def _order(req):
            reached.append(req.raw.get('request_id'))
            return proxy.submit(req.raw, 0.5)

        srv.register('order', _order)
        raw = _order_request(cid, 6207, req_id='req10')
        raw['signed'] = hmac_sign('s3cret', 'req10|%s' % cid)
        resp = srv.dispatch(ManagerRequest(dict(raw)))
        assert reached == ['req10']
        assert resp['status'] == 'unknown'  # no terminal staged
        bad = dict(raw, signed='0' * 64)
        resp = srv.dispatch(ManagerRequest(bad))
        assert resp['status'] == 'reject'
        assert resp['reason'] == 'BAD_SIGNATURE'
        assert reached == ['req10']  # handler never ran


# ---------------------------------------------------------------------------
# 5) Gateway ACK verification over the manager socket
# ---------------------------------------------------------------------------
def _one_shot_server(sock_path, responder):
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    srv.settimeout(15)

    def _run():
        try:
            conn, _ = srv.accept()
            try:
                data = conn.recv(65536).decode()
                req = json.loads(data.strip().splitlines()[0])
                conn.sendall((json.dumps(responder(req)) + '\n').encode())
            finally:
                conn.close()
        finally:
            srv.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _ack_responder(ticket=4242, deal='9001', login=777, echo=True):
    def _resp(req):
        return {'status': 'ack', 'ticket': ticket, 'deal': deal,
                'login': login, 'request_id': req.get('request_id')
                if echo else 'someone-else'}
    return _resp


class TestGatewayVerification:
    def _send(self, tmp_path, monkeypatch, responder, tag):
        sock = str(tmp_path / ('mgr_%s.sock' % tag))
        t = _one_shot_server(sock, responder)
        monkeypatch.setattr(gwmod, 'MANAGER_SOCK', sock)
        log = EventLog(os.path.join(DATA_ROOT, 't_native_gw_events.jsonl'))
        gw = SingleWriterGateway(log)
        cand = {'candidate_id': 'gw_%s_%s' % (tag, uuid.uuid4().hex[:6]),
                'symbol': 'EURUSD.r', 'direction': 'BUY', 'volume': 0.1}
        try:
            return gw.send_order(connection_id='gwconn',
                                 candidate=cand, account={'mt5_login': 777},
                                 broker='fpmarkets', request_timeout=5.0)
        finally:
            t.join(timeout=10)

    def test_genuine_bound_ack_accepted(self, tmp_path, monkeypatch):
        res = self._send(tmp_path, monkeypatch, _ack_responder(), 'ok')
        assert res['outcome'] == 'ACK'
        assert res['receipt_ref']['ticket'] == 4242

    def test_zero_ticket_ack_downgraded(self, tmp_path, monkeypatch):
        res = self._send(tmp_path, monkeypatch,
                         _ack_responder(ticket=0), 't0')
        assert res['outcome'] == 'UNKNOWN'

    def test_missing_deal_ack_downgraded(self, tmp_path, monkeypatch):
        res = self._send(tmp_path, monkeypatch,
                         _ack_responder(deal=''), 'deal')
        assert res['outcome'] == 'UNKNOWN'

    def test_wrong_login_ack_downgraded(self, tmp_path, monkeypatch):
        res = self._send(tmp_path, monkeypatch,
                         _ack_responder(login=1234), 'login')
        assert res['outcome'] == 'UNKNOWN'

    def test_foreign_request_id_ack_downgraded(self, tmp_path, monkeypatch):
        res = self._send(tmp_path, monkeypatch,
                         _ack_responder(echo=False), 'echo')
        assert res['outcome'] == 'UNKNOWN'


# ---------------------------------------------------------------------------
# Helpers: full native-evidence seeding (real MT5_ROOT, cleaned per test)
# ---------------------------------------------------------------------------
def _seed_connected(cid, login, owner, rev='rev-ev'):
    return _seed_account(cid, login, owner, activation_revision=rev,
                         conn_status='CONNECTED', conn_updated_at='t',
                         conn_error='', worker_binding='wb-test',
                         broker_verified_at='t')


def _arm_gateway(gw, cid, rev='rev-ev', broker='deriv'):
    ea = os.path.join(os.path.dirname(__file__), '..', 'mql5',
                      'ApexNativeWriter.mq5')
    return gw.arm(connection_id=cid, ea_path=os.path.normpath(ea),
                  expected_sha256=pin_binary(os.path.normpath(ea)),
                  activation_revision=rev, broker=broker)


def _cleanup_prefix(cid):
    shutil.rmtree(os.path.join(MT5_ROOT, 'prefix-%s' % cid),
                  ignore_errors=True)


# ---------------------------------------------------------------------------
# 6) Native evidence inventory
# ---------------------------------------------------------------------------
class TestNativeEvidence:
    def test_categories_defined(self):
        assert NATIVE_EVIDENCE_CATEGORIES == (
            'broker_session', 'ea_attestation', 'terminal_liveness',
            'positions_inventory', 'exposure_join')

    def test_bare_account_missing_core(self):
        cid, owner = _uid('ev1'), _uid('eown1')
        _seed_account(cid, 6207, owner)
        log = EventLog(os.path.join(DATA_ROOT, 't_native_ev_events.jsonl'))
        ev = assess_native_evidence(cid, gateway=SingleWriterGateway(log))
        assert not ev.complete
        assert ev.missing == ['broker_session', 'ea_attestation',
                              'terminal_liveness']
        assert ev.present['positions_inventory'] is True
        assert ev.present['exposure_join'] is True

    def test_unknown_receipt_breaks_inventory(self):
        cid, owner = _uid('ev2'), _uid('eown2')
        _seed_connected(cid, 6207, owner)
        log = EventLog(os.path.join(DATA_ROOT, 't_native_ev_events.jsonl'))
        gw = SingleWriterGateway(log)
        _arm_gateway(gw, cid)
        _stage_identity(cid, 6207, MT5_ROOT)
        try:
            rm = ReceiptManager(log)
            rm.record_unknown(candidate_id='cand-ev2', connection_id=cid,
                              mt5_login=6207, symbol=SYM, direction='BUY',
                              volume=1.0)
            ev = assess_native_evidence(cid, gateway=gw)
            assert ev.present['positions_inventory'] is False
            # own gaps block via positions_inventory; the join covers
            # siblings only, and this connection has none
            assert ev.present['exposure_join'] is True
            assert not ev.complete
            assert ev.unresolved_unknowns == 1
        finally:
            _cleanup_prefix(cid)

    def test_complete_evidence_all_present(self):
        cid, owner = _uid('ev3'), _uid('eown3')
        _seed_connected(cid, 6207, owner)
        log = EventLog(os.path.join(DATA_ROOT, 't_native_ev_events.jsonl'))
        gw = SingleWriterGateway(log)
        st = _arm_gateway(gw, cid)
        assert st.armed and st.revision_ok
        _stage_identity(cid, 6207, MT5_ROOT)
        try:
            ev = assess_native_evidence(cid, gateway=gw)
            assert ev.complete
            assert ev.missing == []
        finally:
            _cleanup_prefix(cid)

    def test_blind_sibling_breaks_join(self):
        owner = _uid('eown4')
        good, bad = _uid('ev4a'), _uid('ev4b')
        _seed_connected(good, 6207, owner)
        _seed_account(bad, 6208, owner)  # NOT_LINKED but desired ON
        log = EventLog(os.path.join(DATA_ROOT, 't_native_ev_events.jsonl'))
        gw = SingleWriterGateway(log)
        _arm_gateway(gw, good)
        _stage_identity(good, 6207, MT5_ROOT)
        try:
            ev = assess_native_evidence(good, gateway=gw)
            assert ev.missing == ['exposure_join']
            assert bad in ev.reasons['exposure_join']
        finally:
            _cleanup_prefix(good)

    def test_disconnected_sibling_ignored(self):
        owner = _uid('eown5')
        good, gone = _uid('ev5a'), _uid('ev5b')
        _seed_connected(good, 6207, owner)
        _seed_account(gone, 6208, owner, conn_status='DISCONNECTED',
                      desired_on=0, effective_on=0)
        log = EventLog(os.path.join(DATA_ROOT, 't_native_ev_events.jsonl'))
        gw = SingleWriterGateway(log)
        _arm_gateway(gw, good)
        _stage_identity(good, 6207, MT5_ROOT)
        try:
            ev = assess_native_evidence(good, gateway=gw)
            assert ev.complete
        finally:
            _cleanup_prefix(good)


# ---------------------------------------------------------------------------
# 7) Dispatcher evidence gate
# ---------------------------------------------------------------------------
def _seed_reserved(cid, owner, tag):
    dbmod.engine_db.add_candidate({
        'candidate_id': 'cand_%s' % tag, 'connection_id': cid,
        'owner_id': owner, 'broker': 'deriv', 'symbol': SYM,
        'timeframe': 'M5', 'bar_open': '2026-11-16T08:00:00Z',
        'replay_key': 'rk_%s_%s' % (tag, uuid.uuid4().hex[:8]),
        'direction': 'BUY', 'conviction': 0.7, 'regime': 'trend',
        'agent_votes': '{}', 'risk_fraction': 0.15, 'stop_risk_usd': 0.0,
        'volume': 1.0, 'stop_loss': 99.5, 'take_profit': 101.0,
        'price': 100.0, 'entry_type': 'CONFIRM', 'trigger_price': 100.0,
        'invalidation': 99.5, 'entry_quality': 0.8, 'sniper_plan': '{}',
        'execution_state': 'RESERVED', 'reason_code': '', 'created_at': '',
        'updated_at': ''})


class TestDispatcherGate:
    def test_dispatch_blocked_without_evidence(self):
        cid, owner, tag = _uid('dp1'), _uid('down1'), _uid('tag1')
        _seed_account(cid, 6207, owner)
        _seed_reserved(cid, owner, tag)
        log = EventLog(os.path.join(DATA_ROOT, 't_native_dp_events.jsonl'))
        gw = SingleWriterGateway(log)
        disp = NativeDispatcher(log, gw, ReceiptManager(log))
        res = disp.dispatch_account(cid)
        assert res['blocked'] == 1
        assert res['ack'] == 0
        assert res['details'][0]['reason'] == \
            REASON_BLOCKED_INCOMPLETE_EXPOSURE
        assert res['details'][0]['missing']
        # candidates stay RESERVED for a later tick — never silently dropped
        row = dbmod.engine_db.get_candidate('cand_%s' % tag)
        assert row['execution_state'] == 'RESERVED'

    def test_dispatch_sends_with_complete_evidence(self):
        cid, owner, tag = _uid('dp2'), _uid('down2'), _uid('tag2')
        _seed_connected(cid, 6207, owner)
        _seed_reserved(cid, owner, tag)
        log = EventLog(os.path.join(DATA_ROOT, 't_native_dp_events.jsonl'))
        gw = SingleWriterGateway(log)
        _arm_gateway(gw, cid)
        _stage_identity(cid, 6207, MT5_ROOT)

        def _fake_deliver(request, timeout):
            return 'ACK', {'status': 'ack', 'ticket': 555,
                           'deal': '666', 'login': 6207,
                           'request_id': request.get('request_id')}

        gw._deliver_to_ea = _fake_deliver
        try:
            disp = NativeDispatcher(log, gw, ReceiptManager(log))
            res = disp.dispatch_account(cid)
            assert res['ack'] == 1
            assert res['blocked'] == 0
            row = dbmod.engine_db.get_candidate('cand_%s' % tag)
            assert row['execution_state'] == 'ACK'
        finally:
            _cleanup_prefix(cid)
