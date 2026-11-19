"""Issue22 eight-account evidence registry tests — ALL INPUTS SYNTHETIC."""
import copy
import importlib.util
from pathlib import Path
import unittest

P=Path(__file__).resolve().parents[1]/"ops/issue22/eight_native_accounts_ro.py"
spec=importlib.util.spec_from_file_location("issue22_eight_native_qa",P)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def demo(suffix):
    return {
     "schema":"abutron.fpmarkets.native.account_history.readonly.v1",
     "broker":"FP Markets","environment":"DEMO",
     "account_masked":"••••"+suffix,"history_verified_from_mt5":True,
     "history_complete_within_selected_window":True,"account_currency":"USD",
     "orders_submitted_by_exporter":0,"native_file_sha256":"a"*64,
     "history_window_days":90,"history_from_broker_time":100,
     "history_to_broker_time":200,
     "broker_deals_count":3,"trade_deals_count":2,
     "history_trade_deals_shown":2,"closing_deals_count":1,
     "total_trade_deal_cashflow_usd":10.0,
     "closing_deal_cashflow_usd":5.0,
     "balance_usd":100010.0,
     "open_positions_count":0,"pending_orders_count":0,
     "positions":[],
     "trade_deals":[
      {"ticket":"1001","entry":"IN","profit_usd":5.0,"commission_usd":0.0,"fee_usd":0.0,"swap_usd":0.0,"net_cashflow_usd":5.0},
      {"ticket":"1002","entry":"OUT","profit_usd":5.0,"commission_usd":0.0,"fee_usd":0.0,"swap_usd":0.0,"net_cashflow_usd":5.0},
     ],
    }
def deriv():
    data=[]
    for suffix,deals,trades,funding in (("6207",7,6,1),("7593",3,2,1),("9244",5,4,1)):
     data.append({"account_suffix":suffix,"native_broker_fullrange_deals":deals,
       "native_broker_trade_deals":trades,"native_deposit_deals":funding,
       "history_query_from_unix_epoch_zero":True,
       "all_returned_deals_cashflow_to_balance_proved":True,
       "native_broker_cash_balance_usd":10001.0,
       "trading_net_cashflow_usd":1.0,
       "funding_cashflow_usd":10000.0,
       "native_open_positions":0})
    return {"three_native_select_from_zero_fullrange_proved":True,
      "three_native_funding_and_trade_cashflow_balance_reconciled":True,
      "accounts":data,"native_deals_total":15}
def live():
    return {"account_masked":"••••9392","broker":"FP Markets","environment":"LIVE",
      "live_execution_authorized":False,"orders_submitted":0,"crm_switch_modified":False,
      "broker_query_started_at_epoch_zero":True,"current_pending_orders_count":0,
      "broker_returned_fullrange_deals":0,"current_positions_count":0,"current_balance_usd":0.0}
def owner():
    return {"connected_accounts":8,"linked_accounts":8,"native_position_sets_verified":8,
      "unknown_connected_position_sets":0,"status":"BLOCKED_INCOMPLETE_EXPOSURE",
      "blockers":["DAYSTART_UNVERIFIED","FULL_HISTORY_UNVERIFIED","ACK_UNVERIFIED"],
      "live_authorized":False,"confirmed_fp_open_positions":0}
def bundle():
    return [deriv(),{s:demo(s) for s in m.FP_SUFFIXES},live(),owner()]

class EightAccountCoverage(unittest.TestCase):
 def validate(self,mod=None):
    return m.build(*(mod or bundle()))
 def test_eight_account_coverage_never_certifies_lifetime(self):
    x=self.validate()
    self.assertEqual(x["native_account_sources_checked"],8)
    self.assertEqual(x["native_returned_deals_total_observed"],27)
    self.assertEqual(len(x["accounts"]),8)
    self.assertFalse(x["all_eight_issuer_certified_lifetime"])
    self.assertFalse(x["broker_order_ack_and_chandelier_certified"])
    self.assertFalse(x["live_trading_modified"])
    self.assertEqual(x["owner_status_unchanged"],"BLOCKED_INCOMPLETE_EXPOSURE")

 def test_undocumented_fp_account_not_permitted(self):
    a=bundle();a[1]["9999"]=demo("9999")
    with self.assertRaisesRegex(m.AccountCoverageError,"FP_DEMO_HISTORY_SET_NOT_FOUR"):
     self.validate(a)

 def test_fp_wrong_account_mask_denied(self):
    a=bundle();a[1]["0512"]["account_masked"]="••••9999"
    with self.assertRaisesRegex(m.AccountCoverageError,"FP_NATIVE_ACCOUNT_AUTHENTICATION_FAILED"):
     self.validate(a)

 def test_fp_broker_source_digest_required(self):
    a=bundle();a[1]["2582"]["native_file_sha256"]="unknown"
    with self.assertRaisesRegex(m.AccountCoverageError,"FP_NATIVE_ORIGINAL_SOURCE_SHA_MISSING"):
     self.validate(a)

 def test_duplicate_fp_trade_ticket_rejected(self):
    a=bundle();a[1]["1636"]["trade_deals"][1]["ticket"]="1001"
    with self.assertRaisesRegex(m.AccountCoverageError,"FP_DUPLICATE_NATIVE_DEAL_TICKET"):
     self.validate(a)

 def test_fp_deal_net_must_match_components(self):
    a=bundle();a[1]["1622"]["trade_deals"][0]["net_cashflow_usd"]=10.0
    with self.assertRaisesRegex(m.AccountCoverageError,"FP_DEAL_COMPONENTS_DO_NOT_RECONCILE"):
     self.validate(a)

 def test_fp_sum_must_match_broker_export(self):
    a=bundle();a[1]["1622"]["total_trade_deal_cashflow_usd"]=9.0
    with self.assertRaisesRegex(m.AccountCoverageError,"FP_BROKER_NET_OR_CLOSING_CASHFLOW_DRIFT"):
     self.validate(a)

 def test_fp_missing_protective_stop_rejected(self):
    a=bundle();a[1]["0512"]["positions"]=[{"stop_loss":0.0,"take_profit":1.0}]
    a[1]["0512"]["open_positions_count"]=1;a[3]["confirmed_fp_open_positions"]=1
    with self.assertRaisesRegex(m.AccountCoverageError,"FP_OPEN_POSITION_MISSING_BROKER_PROTECTION"):
     self.validate(a)

 def test_owner_native_set_cannot_be_seven(self):
    a=bundle();a[3]["native_position_sets_verified"]=7
    with self.assertRaisesRegex(m.AccountCoverageError,"OWNER_CURRENT_POSITION_INVENTORY_NOT_VERIFIED"):
     self.validate(a)

 def test_owner_risk_certification_not_mutated(self):
    a=bundle();a[3]["status"]="CERTIFIED"
    with self.assertRaisesRegex(m.AccountCoverageError,"OWNER_EXPECTED_HISTORICAL_BLOCKERS_CHANGED"):
     self.validate(a)

 def test_deriv_balance_gap_detected(self):
    a=bundle();a[0]["accounts"][0]["native_broker_cash_balance_usd"]=10002.0
    with self.assertRaisesRegex(m.AccountCoverageError,"DERIV_NATIVE_ACCOUNT_BALANCE_MISMATCH"):
     self.validate(a)

 def test_missing_deriv_account_detected(self):
    a=bundle();a[0]["accounts"][0]["account_suffix"]="0000"
    with self.assertRaisesRegex(m.AccountCoverageError,"DERIV_ORIGINAL_ACCOUNT_IDENTITY_MISMATCH"):
     self.validate(a)

 def test_deriv_total_deal_count_mismatch_rejected(self):
    a=bundle();a[0]["native_deals_total"]=17
    with self.assertRaisesRegex(m.AccountCoverageError,"DERIV_THREE_NATIVE_DEAL_TOTAL_INVALID"):
     self.validate(a)

 def test_live_cannot_become_active_through_observer(self):
    a=bundle();a[2]["live_execution_authorized"]=True
    with self.assertRaisesRegex(m.AccountCoverageError,"LIVE_NATIVE_READONLY_IDENTITY_UNVERIFIED"):
     self.validate(a)

 def test_live_zero_deals_never_lifetime_certified(self):
    x=self.validate()
    self.assertEqual(x["accounts"][-1]["evidence_class"],"BROKER_NATIVE_EPOCH_ZERO_ZERO_DEALS_UNCERTIFIED")
    self.assertFalse(x["accounts"][-1]["issuer_independent_full_account_lifetime_certified"])

 def test_native_position_cross_match_mandatory(self):
    a=bundle();a[3]["confirmed_fp_open_positions"]=1
    with self.assertRaisesRegex(m.AccountCoverageError,"BROKER_ACCOUNT_OPEN_POSITIONS_DO_NOT_MATCH_OWNER"):
     self.validate(a)

 def registration_fixture(self):
    a=bundle()
    rows=[]
    for suffix in m.FP_SUFFIXES:
      a[1][suffix]["history_from_broker_time"]=1785000000+7200
      a[1][suffix]["history_to_broker_time"]=1795100000+7200
      a[1][suffix]["broker_clock_offset_seconds"]=7200
      rows.append((suffix,1794500000,"FPMarketsSC-Demo"))
    return a,rows

 def test_all_four_crm_registrations_inside_native_window_not_issuer_inception(self):
    accounts,rows=self.registration_fixture()
    r=m.crm_window_check(m.build(*accounts),accounts[1],rows)
    self.assertEqual(r["crm_registration_covered_by_native_window_count"],4)
    self.assertFalse(r["all_eight_issuer_certified_lifetime"])
    for row in r["accounts"]:
      if row["broker"]=="FP Markets" and row["environment"]=="DEMO":
        self.assertTrue(row["native_90day_history_spans_local_crm_registration"])
        self.assertGreater(row["window_earlier_than_crm_registration_days"],1)
        self.assertFalse(row["crm_registration_is_broker_issued_inception"])

 def test_duplicate_crm_account_suffix_rejected(self):
    accounts,rows=self.registration_fixture()
    rows[1]=rows[0]
    with self.assertRaises(m.AccountCoverageError):
      m.crm_window_check(m.build(*accounts),accounts[1],rows)

 def test_crm_server_must_match_broker(self):
    accounts,rows=self.registration_fixture()
    rows[2]=(rows[2][0],rows[2][1],"FPMarketsSC-Live")
    with self.assertRaisesRegex(m.AccountCoverageError,"CRM_ACCOUNT_IDENTITY"):
      m.crm_window_check(m.build(*accounts),accounts[1],rows)

 def test_native_clock_must_be_attested(self):
    accounts,rows=self.registration_fixture()
    accounts[1]["1636"]["broker_clock_offset_seconds"]=0
    with self.assertRaisesRegex(m.AccountCoverageError,"BROKER_CLOCK_OFFSET_NOT_AUTHENTICATED"):
      m.crm_window_check(m.build(*accounts),accounts[1],rows)

 def test_history_starting_after_crm_creation_refused(self):
    accounts,rows=self.registration_fixture()
    accounts[1]["2582"]["history_from_broker_time"]=1794700000+7200
    with self.assertRaisesRegex(m.AccountCoverageError,"DOES_NOT_SPAN_CRM"):
      m.crm_window_check(m.build(*accounts),accounts[1],rows)

 def test_created_at_inferred_funding_not_promoted_to_deposit(self):
    accounts,rows=self.registration_fixture()
    d=m.crm_window_check(m.build(*accounts),accounts[1],rows)
    for rec in d["accounts"]:
      if rec["broker"]=="FP Markets" and rec["environment"]=="DEMO":
        self.assertTrue(rec["initial_funding_is_inferred_not_native_issuer_proved"])
        self.assertFalse(rec["issuer_independent_full_account_lifetime_certified"])

 def test_no_broker_mutation_api(self):
    text=P.read_text()
    for banned in ("OrderSend(","TRADE_ACTION_DEAL","TRADE_ACTION_SLTP",
                   "systemctl stop","subprocess.run","os.system("):
     self.assertNotIn(banned,text)

if __name__=="__main__":
 unittest.main()
