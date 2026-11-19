"""Issue44 isolated read-only MT5 restart position comparison tests."""
import importlib.util
from pathlib import Path
import unittest
import hashlib

FILE=Path(__file__).resolve().parents[1]/"ops/issue44/compare_native_restart_ro.py"
spec=importlib.util.spec_from_file_location("issue44_native_restart_ro",FILE)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def export(utc=1795000000,nonce="a"*32,login="7410512",positions=None,deals=None,orders=None):
    if positions is None:
        positions=[
            "P\t10001\tGER40\t1\t0.360000\t25700.000000\t25620.000000\t25760.000000\t25200.000000\t30.000000\t0.000000\t1794990000\t1794991000",
            "P\t10002\tEURUSD.r\t0\t0.300000\t1.098000\t1.099000\t1.094000\t1.105000\t10.000000\t0.000000\t1794991000\t1794992000",
        ]
    if deals is None:deals=[]
    if orders is None:orders=[]
    top=[
        "ABUTRON_FP_RESTART_SNAPSHOT_V103",nonce,"b"*36,
        "FPMarketsSC-Demo",login,"DEMO",str(utc),"7200","USD",
        "100000.000000","100100.000000","1000.000000","99000.000000","100.000000",
        str(len(positions)),str(len(orders)),str(len(deals)),
        "0",str(utc+7260),"BROKER_NATIVE_NO_ORDER_FUNCTIONS"
    ]
    tail=["END\t%d\t%d\t%d"%(len(positions),len(orders),len(deals))]
    return ("\n".join(top+positions+orders+deals+tail)+"\n").encode()

class RestartNativeSnapshotContracts(unittest.TestCase):
    def test_authentic_broker_before_after_stops_preserved(self):
        a=m.parse_native(export())
        b=m.parse_native(export(utc=1795000060))
        r=m.compare(a,b)
        self.assertEqual(r["status"],"ISOLATED_BROKER_POSITION_CONTINUITY_OBSERVED")
        self.assertEqual(r["positions_confirmed_after_restart"],2)
        self.assertEqual(r["positions_closed_with_native_deal_evidence"],0)
        self.assertEqual(r["before_pending_orders"],0)
        self.assertIs(r["production_release_certified"],False)

    def test_monotonic_buy_and_sell_stops_allowed(self):
        after=export(utc=1795000060,positions=[
            "P\t10001\tGER40\t1\t0.360000\t25700.000000\t25620.000000\t25740.000000\t25200.000000\t30.000000\t0.000000\t1794990000\t1794991000",
            "P\t10002\tEURUSD.r\t0\t0.300000\t1.098000\t1.099000\t1.095000\t1.105000\t10.000000\t0.000000\t1794991000\t1794992000",
        ])
        self.assertEqual(m.compare(m.parse_native(export()),m.parse_native(after))["positions_confirmed_after_restart"],2)

    def test_widening_buy_stop_rejected(self):
        after=export(utc=1795000060,positions=[
            "P\t10001\tGER40\t1\t0.360000\t25700.000000\t25620.000000\t25760.000000\t25200.000000\t30.000000\t0.000000\t1794990000\t1794991000",
            "P\t10002\tEURUSD.r\t0\t0.300000\t1.098000\t1.099000\t1.092000\t1.105000\t10.000000\t0.000000\t1794991000\t1794992000",
        ])
        with self.assertRaisesRegex(m.NativeRestartEvidenceError,"RECONNECTED_STOP_WIDENED"):
            m.compare(m.parse_native(export()),m.parse_native(after))

    def test_widening_sell_stop_rejected(self):
        after=export(utc=1795000060,positions=[
            "P\t10001\tGER40\t1\t0.360000\t25700.000000\t25620.000000\t25780.000000\t25200.000000\t30.000000\t0.000000\t1794990000\t1794991000",
            "P\t10002\tEURUSD.r\t0\t0.300000\t1.098000\t1.099000\t1.094000\t1.105000\t10.000000\t0.000000\t1794991000\t1794992000",
        ])
        with self.assertRaisesRegex(m.NativeRestartEvidenceError,"RECONNECTED_STOP_WIDENED"):
            m.compare(m.parse_native(export()),m.parse_native(after))

    def test_missing_position_without_native_exit_fail_closed(self):
        after=export(utc=1795000060,positions=[
            "P\t10001\tGER40\t1\t0.360000\t25700.000000\t25620.000000\t25760.000000\t25200.000000\t30.000000\t0.000000\t1794990000\t1794991000",
        ])
        with self.assertRaisesRegex(m.NativeRestartEvidenceError,"MISSING_POSITION_WITHOUT_AUTHENTIC_NATIVE_EXIT"):
            m.compare(m.parse_native(export()),m.parse_native(after))

    def test_closure_only_accepted_with_authenticated_exit_timestamp(self):
        dea="D\t30001\t40001\t10002\t1795007250\t1\t1\t0\tEURUSD.r\t0.300000\t1.100000\t10.000000\t0\t0\t0"
        after=export(utc=1795000060,positions=[
            "P\t10001\tGER40\t1\t0.360000\t25700.000000\t25620.000000\t25760.000000\t25200.000000\t30.000000\t0.000000\t1794990000\t1794991000",
        ],deals=[dea])
        r=m.compare(m.parse_native(export()),m.parse_native(after))
        self.assertEqual(r["positions_closed_with_native_deal_evidence"],1)

    def test_old_exit_is_not_accepted_as_new_closure(self):
        dea="D\t30001\t40001\t10002\t1794990000\t1\t1\t0\tEURUSD.r\t0.300000\t1.100000\t10.000000\t0\t0\t0"
        after=export(utc=1795000060,positions=[
            "P\t10001\tGER40\t1\t0.360000\t25700.000000\t25620.000000\t25760.000000\t25200.000000\t30.000000\t0.000000\t1794990000\t1794991000",
        ],deals=[dea])
        with self.assertRaisesRegex(m.NativeRestartEvidenceError,"MISSING_POSITION"):
            m.compare(m.parse_native(export()),m.parse_native(after))

    def test_identity_nonce_or_login_change_rejected(self):
        for altered in (export(utc=1795000060,nonce="f"*32),
                        export(utc=1795000060,login="7411636")):
            with self.assertRaisesRegex(m.NativeRestartEvidenceError,"ACCOUNT_SESSION_MISMATCH"):
                m.compare(m.parse_native(export()),m.parse_native(altered))

    def test_pending_order_prevents_certification(self):
        pending="O\t20001\tGER40\t1\t1\t0.360000\t0.360000\t25700.000000\t25760.000000\t25200.000000\t1795000000"
        with self.assertRaisesRegex(m.NativeRestartEvidenceError,"PENDING_BROKER_ORDERS"):
            m.compare(m.parse_native(export(orders=[pending])),m.parse_native(export(utc=1795000060)))

    def test_incomplete_snapshot_or_count_truncated_rejected(self):
        raw=export()
        for altered in (raw[:-15],raw.replace(b"END\t2\t0\t0",b"END\t1\t0\t0")):
            with self.assertRaises(m.NativeRestartEvidenceError):
                m.parse_native(altered)

    def test_duplicate_position_rejected(self):
        base=export().splitlines()
        duplicate=next(x for x in base if x.startswith(b"P\t"))
        index=next(i for i,x in enumerate(base) if x.startswith(b"END\t"))
        base.insert(index,duplicate)
        base[14]=b"3"
        base[-1]=b"END\t3\t0\t0"
        with self.assertRaisesRegex(m.NativeRestartEvidenceError,"DUPLICATED_BROKER_POSITION"):
            m.parse_native(b"\n".join(base)+b"\n")

    def test_reconnect_zero_deals_rejected_if_before_had_history(self):
        original="D\\t301\\t401\\t10001\\t1794990000\\t1\\t0\\t0\\tGER40\\t0.360000\\t25700.000000\\t0\\t0\\t0\\t0"
        before=m.parse_native(export(deals=[original]))
        after=m.parse_native(export(utc=1795000060,deals=[]))
        with self.assertRaisesRegex(m.NativeRestartEvidenceError,"BROKER_HISTORY_NOT_REHYDRATED"):
            m.compare(before,after)

    def test_same_count_with_substituted_history_ticket_rejected(self):
        a="D\\t301\\t401\\t10001\\t1794990000\\t1\\t0\\t0\\tGER40\\t0.360000\\t25700.000000\\t0\\t0\\t0\\t0"
        b=a.replace("D\\t301\\t","D\\t302\\t")
        with self.assertRaisesRegex(m.NativeRestartEvidenceError,"BROKER_HISTORY_NOT_REHYDRATED"):
            m.compare(m.parse_native(export(deals=[a])),
                      m.parse_native(export(utc=1795000060,deals=[b])))

    def test_historical_deal_ids_preserved_after_restart(self):
        a="D\\t301\\t401\\t10001\\t1794990000\\t1\\t0\\t0\\tGER40\\t0.360000\\t25700.000000\\t0\\t0\\t0\\t0"
        b=a.replace("D\\t301\\t","D\\t302\\t")
        r=m.compare(m.parse_native(export(deals=[a])),
                    m.parse_native(export(utc=1795000060,deals=[a,b])))
        self.assertEqual(r["historical_deals_verified_before"],1)
        self.assertEqual(r["historical_deals_verified_after"],2)
        self.assertIs(r["observed_history_deal_ids_preserved_after_restart"],True)

    def test_native_mql_hydration_threshold_is_not_trading_authority(self):
        source=(FILE.parents[1]/"mql5/AbutronFPPositionRestartSnapshotRO.mq5").read_text()
        self.assertIn("MinBrokerDealsToPublish=0",source)
        self.assertIn("dcount<MinBrokerDealsToPublish",source)
        self.assertIn("FP_RESTART_HISTORY_HYDRATION_PENDING",source)
        for forbidden in ("OrderSend(","OrderCheck(","TRADE_ACTION_SLTP","CTrade"):
            self.assertNotIn(forbidden,source)

    def test_no_broker_mutation_capability(self):
        source=FILE.read_text()
        for forbidden in ("OrderSend(", "OrderCheck(", "TRADE_ACTION_SLTP",
                          "subprocess.run", "os.system(", "systemctl stop"):
            self.assertNotIn(forbidden,source)
        self.assertIn('"running_production_MT5_restarted":False',source)
        self.assertIn('"production_release_certified":False',source)

if __name__=="__main__":
    unittest.main()
