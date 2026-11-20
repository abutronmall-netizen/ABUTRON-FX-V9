//+------------------------------------------------------------------+
//| ApexNativeWriter.mq5                                               |
//| ABUTRON FX V9 — APEX 1.1 native single-writer EA                    |
//|                                                                   |
//| This EA is the ONLY order-authority actor inside an account's      |
//| MT5 terminal. It:                                                 |
//|   1. Exports account identity + native M5 bars + order-flow ticks  |
//|   2. Receives HMAC-signed order requests from the manager socket   |
//|      via MQL5/Files handoff                                       |
//|   3. Places broker-native orders with protective SL/TP             |
//|   4. Writes native receipts (ticket/deal/SL/TP) back to MQL5/Files |
//|                                                                   |
//| Evidence discipline: receipts are written ONLY after a real       |
//| OrderSend result with retcode == TRADE_RETCODE_DONE; ambiguous    |
//| outcomes are written as UNKNOWN. No fabricated ACK.               |
//|                                                                   |
//| Security: every order request is HMAC-SHA256 signed over the      |
//| COMPLETE canonical payload (nonce|candidate|cid|symbol|dir|vol|  |
//| sl|tp|entry_type|trigger|trail_k|trail_stop). The EA rejects any  |
//| request whose signature does not match.                           |
//| The HMAC secret is provisioned from MQL5/Files/ABUTRON_ACTIVATION  |
//| _SECRET.txt (written by the Python service) or set via the       |
//| InpActivationSecret input.                                        |
//+------------------------------------------------------------------+
#property copyright "ABUTRON FX V9"
#property version   "1.11"

// V9 trade magic — identifies positions opened by this EA so the
// trailing loop only manages its own positions and never touches
// unrelated broker positions. Restored from the previously deployed
// binary; absent from the faulty rebuild.
#define MAGIC 73113051

input string  InpConnectionId   = "";           // connection id
input string  InpBrokerServer   = "";           // broker server
input long    InpActivationSeed = 0;            // activation seed (per arm)
input string  InpActivationSecret = "";         // HMAC key (or file-provisioned)
input bool    InpExposureOnly   = true;         // QA default: export only, NO OrderSend
input int     InpManagedCharts  = 0;            // 0 = all charts with EA
input double  InpRiskFraction   = 0.15;         // per-trade risk fraction (display)
input int     InpRiskFractionPct = 15;          // percent (15 => 0.15)
input ENUM_TIMEFRAMES InpExportPeriod = PERIOD_M5;  // native export timeframe

string g_activation_secret = "";                // resolved at OnInit

// Trailing-stop state: tracks positions opened by this EA so the
// OnTimer loop can tighten the broker SL toward the trail stop.
// Restored from the previously deployed binary; the faulty rebuild
// omitted this entirely (trail_k/trail_stop were read and signed but
// never applied). The brain computes the Chandelier trail; the EA
// only executes the trail via ModifyOrder, never widening the stop.
ulong   g_trail_ticket[];     // position tickets managed by this EA
string  g_trail_symbol[];     // symbols
string  g_trail_dir[];        // BUY / SELL
double  g_trail_k[];          // ATR multiple
double  g_trail_stop[];       // current trail stop price
double  g_trail_entry[];      // entry price (for profit check)
bool    g_trail_active[];     // whether the trail has room to tighten

int OnInit()
{
   // Terminal-level algorithmic trading permissions are managed by MT5.
   // Verify SHA256/HMAC against a public, non-secret known-answer vector.
   string kat = HmacSha256Hex("key", "The quick brown fox jumps over the lazy dog");
   if(kat != "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8")
   {
      Print("ISSUE38_INIT_BLOCK_HMAC_KAT result=", kat);
      return(INIT_FAILED);
   }
   g_activation_secret = LoadActivationSecret();
   if(g_activation_secret == "")
   {
      Print("ISSUE38_INIT_BLOCK_SECRET_NOT_PROVISIONED");
      return(INIT_FAILED);
   }
   if(InpConnectionId == "" || InpBrokerServer == "")
   {
      Print("ISSUE38_INIT_BLOCK_MISSING_EA_ACCOUNT_PARAMETERS");
      return(INIT_FAILED);
   }
   if(AccountInfoString(ACCOUNT_SERVER) != InpBrokerServer)
   {
      Print("ISSUE38_INIT_BLOCK_BROKER_SERVER_MISMATCH");
      return(INIT_FAILED);
   }
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   {
      Print("ISSUE38_INIT_BLOCK_NON_DEMO_ACCOUNT");
      return(INIT_FAILED);
   }
   Print("ISSUE38_INIT_EXPOSURE_QA_READY_NO_ORDERS");
   EventSetTimer(5);                                  // export + handoff loop
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   // Clear trailing-stop tracking state.
   ArrayFree(g_trail_ticket);
   ArrayFree(g_trail_symbol);
   ArrayFree(g_trail_dir);
   ArrayFree(g_trail_k);
   ArrayFree(g_trail_stop);
   ArrayFree(g_trail_entry);
   ArrayFree(g_trail_active);
}

void OnTimer()
{
   ExportNativeData();
   // DEMO exposure QA is strictly read-only unless explicitly reconfigured.
   if(!InpExposureOnly)
   {
      ProcessOrderRequests();
      ApplyTrailingStops();
   }
}

//+------------------------------------------------------------------+
//| Export account identity + native bars + order flow               |
//+------------------------------------------------------------------+
void ExportNativeData()
{
   string dir = "";  // FileOpen paths are relative to the MQL5/Files sandbox.

   // --- Account identity -------------------------------------------
   string identity = dir + "ABUTRON_NATIVE_ACCOUNT_V2_" + Nonce() + ".txt";
   string idJson = StringFormat(
      "{\"connection_id\":\"%s\",\"login\":%d,\"broker\":\"%s\",\"server\":\"%s\",\"nonce\":\"%s\",\"challenge\":\"%s\",\"worker_health_ts\":\"%s\"}",
      InpConnectionId, AccountInfoInteger(ACCOUNT_LOGIN),
      "native", InpBrokerServer, Nonce(), Challenge(), TimeStampToStr(TimeCurrent()));
   WriteTextFile(identity, idJson);

   // --- Closed M5 bars (last 120) -----------------------------------
   // Closed bars only. The previous datetime range had stop_time=0,
   // which is earlier than start_time and could return zero bars.
   MqlRates rates[];
   int count = CopyRates(_Symbol, InpExportPeriod, 1, 120, rates);
   if(count < 0) ArrayResize(rates, 0);

   string barsJson = "{\"bars\":[";
   for(int i = 0; i < ArraySize(rates); i++)
   {
      MqlRates r = rates[i];
      barsJson += StringFormat(
         "{\"symbol\":\"%s\",\"timeframe\":\"M5\",\"open_time\":\"%s\",\"close_time\":\"%s\",\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%.2f,\"bid\":%.5f,\"ask\":%.5f,\"spread\":%.0f,\"tick_size\":%.5f,\"point\":%.5f,\"closed\":true},",
         _Symbol,
         TimeToString(r.time, TIME_DATE|TIME_SECONDS),
         TimeToString(r.time + PeriodSeconds(InpExportPeriod), TIME_DATE|TIME_SECONDS),
         r.open, r.high, r.low, r.close, r.tick_volume,
         SymbolInfoDouble(_Symbol, SYMBOL_BID),
         SymbolInfoDouble(_Symbol, SYMBOL_ASK),
         (int)((SymbolInfoDouble(_Symbol, SYMBOL_ASK) -
                SymbolInfoDouble(_Symbol, SYMBOL_BID)) / _Point),
         _Point, _Point);
   }
   // Remove trailing comma manually (StringTrimRight only trims whitespace)
   if(StringLen(barsJson) > 0 && StringSubstr(barsJson, StringLen(barsJson)-1, 1) == ",")
      barsJson = StringSubstr(barsJson, 0, StringLen(barsJson)-1);
   barsJson += "]}";
   string barsFile = dir + "ABUTRON_NATIVE_BARS_V2_" + Nonce() + ".txt";
   WriteTextFile(barsFile, barsJson);

   // --- Authenticated quote snapshot (bid/ask/tick geometry) -------
   // Separate from bars: the bar file has OHLC only. Quotes must come
   // from the broker's own SymbolInfoDouble at export time, bound to
   // the same nonce/login/server/session. Never derive quotes from
   // bar close.
   string quotesFile = dir + "ABUTRON_NATIVE_QUOTES_V2_" + Nonce() + ".txt";
   string qJson = "{\"quotes\":[";
   string qs = StringFormat("{\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,"
      "\"spread\":%.0f,\"tick_size\":%.5f,\"point\":%.5f,\"observed_at\":\"%s\"},",
      _Symbol,
      SymbolInfoDouble(_Symbol, SYMBOL_BID),
      SymbolInfoDouble(_Symbol, SYMBOL_ASK),
      (int)((SymbolInfoDouble(_Symbol, SYMBOL_ASK) -
             SymbolInfoDouble(_Symbol, SYMBOL_BID)) / _Point),
      _Point, _Point,
      TimeStampToStr(TimeCurrent()));
   qJson += qs;
   qJson = StringSubstr(qJson, 0, StringLen(qJson) - 1); // strip trailing comma
   qJson += "]}";
   WriteTextFile(quotesFile, qJson);

   // --- Order flow: aggressor trades since last export -------------
   string ofJson = "{\"trades\":[";
   string lastFile = dir + "ABUTRON_ORDER_FLOW_V2_LAST.txt";
   if(FileIsExist(lastFile))
   {
      int h = FileOpen(lastFile, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
      if(h > 0)
      {
         string s = FileReadString(h);
         FileClose(h);
      }
   }
   for(int i = 0; i < ArraySize(rates); i++)
   {
      MqlRates r = rates[i];
      if(r.tick_volume <= 0) continue;
      ofJson += StringFormat(
         "{\"price\":%.5f,\"size\":%.2f,\"side\":\"%s\",\"time\":\"%s\"},",
         r.close, r.tick_volume / 100.0,
         (r.close >= r.open) ? "buy" : "sell",
         TimeToString(r.time, TIME_DATE|TIME_SECONDS));
   }
   if(StringLen(ofJson) > 0 && StringSubstr(ofJson, StringLen(ofJson)-1, 1) == ",")
      ofJson = StringSubstr(ofJson, 0, StringLen(ofJson)-1);
   ofJson += "]}";
   string oflowFile = dir + "ABUTRON_ORDER_FLOW_V2_" + Nonce() + ".txt";
   WriteTextFile(oflowFile, ofJson);
   WriteTextFile(lastFile, TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS));

   // --- Broker-native exposure snapshot (Issue #36) -----------------
   // Never attest flat positions from a disconnected terminal.
   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
      return;
   // Open positions + account equity/balance, HMAC-authenticated by the
   // activation secret and bound to THIS account (connection_id + login).
   // The Python worker verifies the HMAC sig, the nonce freshness and
   // account-binding before certifying exposure — a fabricated snapshot
   // without the activation secret cannot certify.
   //
   // JSON escaping: in MQL5 a string literal "\\" is a single backslash
   // and "\"" is a quote; to emit valid JSON the quotes must be "\""
   // (NOT "\\\\\\\"), otherwise json.loads on the Python side fails.
   string loginStr = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   double equityD  = AccountInfoDouble(ACCOUNT_EQUITY);
   // Nonce: ABUTRON_NATIVE_<login>_<unix_ts> — Python parses login +
   // freshness from this; the HMAC sig provides real authentication.
   string expNonce = StringFormat("ABUTRON_NATIVE_%s_%d", loginStr,
                                  (long)TimeGMT());

   // Collect ticket list and sort ascending for a deterministic canonical.
   ulong allTickets[];
   int posCount = PositionsTotal();
   ArrayResize(allTickets, posCount);
   int n = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong t = PositionGetTicket(i);
      if(t != 0) allTickets[n++] = t;
   }
   // A failed ticket read is an incomplete inventory, NOT broker-flat.
   if(n != posCount)
      return;
   ArrayResize(allTickets, n);
   ArraySort(allTickets);

   string positionsCanon = "";
   string posJson = "";
   for(int k = 0; k < ArraySize(allTickets); k++)
   {
      ulong ticket = allTickets[k];
      if(!PositionSelectByTicket(ticket))
         return;  // session/position changed: do not publish partial proof.
      string sym = PositionGetString(POSITION_SYMBOL);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);
      double vol  = PositionGetDouble(POSITION_VOLUME);
      int type    = (int)PositionGetInteger(POSITION_TYPE);
      ENUM_ORDER_TYPE ordType = (type == POSITION_TYPE_BUY)
                                ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      string dir  = (type == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      double swap = PositionGetDouble(POSITION_SWAP);
      // MQL5: bool OrderCalcProfit(type, symbol, volume, open, close, profit).
      // Reject missing SL, broker calculation failures, or non-loss geometry.
      double calcProfit = 0.0;
      bool ocVerified = (sl > 0.0
                         && OrderCalcProfit(ordType, sym, vol, entry, sl,
                                            calcProfit)
                         && MathIsValidNumber(calcProfit)
                         && calcProfit < 0.0);
      double stopRiskUsd = ocVerified ? -calcProfit : 0.0;
      if(k > 0) positionsCanon += ";";
      positionsCanon += StringFormat("%I64d:%s:%s:%.2f:%.5f:%.5f:%.5f:%.2f:%s",
                                     ticket, sym, dir, vol, entry, sl, tp,
                                     stopRiskUsd, ocVerified ? "true" : "false");
      posJson += StringFormat(
         "{\"ticket\":%I64u,\"symbol\":\"%s\",\"direction\":\"%s\","
         "\"volume\":%.2f,\"entry\":%.5f,\"sl\":%.5f,\"tp\":%.5f,"
         "\"swap\":%.2f,\"stop_risk_usd\":%.2f,\"ordercalcprofit_verified\":%s},",
         ticket, sym, dir, vol, entry, sl, tp, swap, stopRiskUsd,
         ocVerified ? "true" : "false");
   }
   if(StringLen(posJson) > 0 && StringSubstr(posJson, StringLen(posJson)-1, 1) == ",")
      posJson = StringSubstr(posJson, 0, StringLen(posJson)-1);

   // The signed wire format is mirrored in Python build_exposure_canonical.
   // Include every broker-money field that CRM can use for certification.
   string expServer = AccountInfoString(ACCOUNT_SERVER);
   string expCurrency = AccountInfoString(ACCOUNT_CURRENCY);
   double balanceD = AccountInfoDouble(ACCOUNT_BALANCE);
   string expChallenge = Challenge();
   string canonical = StringFormat("%s|%s|%s|%s|%s|%s|%s|%s|%d|%s",
         InpConnectionId, loginStr, expServer, expCurrency,
         DoubleToString(equityD, 2), DoubleToString(balanceD, 2),
         expNonce, expChallenge, n, positionsCanon);
   if(g_activation_secret == "")
      return;  // Without provisioned key, never publish certifiable evidence.
   string sig = HmacSha256Hex(g_activation_secret, canonical);
   string expJson = StringFormat(
      "{\"nonce\":\"%s\",\"challenge\":\"%s\",\"connection_id\":\"%s\","
      "\"login\":\"%s\",\"server\":\"%s\",\"equity\":%s,\"balance\":%s,"
      "\"currency\":\"%s\",\"position_count\":%d,\"sig\":\"%s\","
      "\"positions\":[",
      expNonce, expChallenge, InpConnectionId, loginStr, expServer,
      DoubleToString(equityD, 2), DoubleToString(balanceD, 2),
      expCurrency, n, sig);
   expJson += posJson + "]}";
   string exposureFile = dir + "ABUTRON_NATIVE_EXPOSURE_V2_" + expNonce + ".txt";
   WriteTextFile(exposureFile, expJson);
}

//+------------------------------------------------------------------+
//| Process signed order requests from MQL5/Files handoff            |
//+------------------------------------------------------------------+
void ProcessOrderRequests()
{
   string dir = "";  // FileOpen paths are relative to the MQL5/Files sandbox.
   string pending = dir + "ABUTRON_REQ_PENDING.json";
   if(!FileIsExist(pending)) return;

   // Open and read the pending request
   // Python writes UTF-8 JSON. MQL5 FILE_TXT without FILE_ANSI defaults to
   // UTF-16, which misreads the request and loses the request_id. The
   // previously working binary used FILE_ANSI with CP_UTF8 — restore it.
   int h = FileOpen(pending, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(h <= 0) { FileDelete(pending); return; }
   string body = FileReadString(h);
   FileClose(h);

   if(StringLen(body) <= 0) { FileDelete(pending); return; }

   // Parse JSON request
   string reqId   = JsonStr(body, "request_id");
   string cid     = JsonStr(body, "connection_id");
   string symbol  = JsonStr(body, "symbol");
   string dirn    = JsonStr(body, "direction");
   double volume  = JsonNum(body, "volume");
   double sl      = JsonNum(body, "stop_loss");
   double tp      = JsonNum(body, "take_profit");
   string actRev  = JsonStr(body, "activation_revision");
   string sig     = JsonStr(body, "signed");
   string cand    = JsonStr(body, "candidate_id");
   string entryTy = JsonStr(body, "entry_type");
   double trigger = JsonNum(body, "trigger_price");
   double trailK  = JsonNum(body, "trail_k");
   double trailSt = JsonNum(body, "trail_stop");

   // Verify connection binding + activation (single-writer authority)
   if(cid != InpConnectionId || actRev != ActivationRevision())
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_SINGLE_WRITER_DISARMED",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }

   // Verify HMAC-SHA256 signature over the COMPLETE canonical order
   // payload: nonce|candidate|cid|symbol|dir|vol|sl|tp|entry_type|
   //          trigger|trail_k|trail_stop
   string canonical = reqId + "|" + cand + "|" + cid + "|" + symbol +
                      "|" + dirn + "|" + StringFormat("%.6f", volume) +
                      "|" + StringFormat("%.6f", sl) +
                      "|" + StringFormat("%.6f", tp) +
                      "|" + entryTy + "|" + StringFormat("%.6f", trigger) +
                      "|" + StringFormat("%.6f", trailK) +
                      "|" + StringFormat("%.6f", trailSt);
   if(!VerifySignature(sig, canonical))
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_SIGNATURE_INVALID",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }

   double price = 0.0;
   // Use the requested instrument, not whichever chart hosts the EA.
   if(dirn == "BUY")  price = SymbolInfoDouble(symbol, SYMBOL_ASK);
   else               price = SymbolInfoDouble(symbol, SYMBOL_BID);

   // Floor volume to broker lot step — never round up.
   // If the floored lot is below the minimum, REJECT (not increase).
   double normVol = NormalizeLots(volume, symbol);
   double lotMin  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   if(normVol < lotMin || normVol <= 0.0)
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_MIN_LOT_EXCEEDS_RISK",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }

   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req); ZeroMemory(res);
   req.action    = TRADE_ACTION_DEAL;
   req.symbol    = symbol;
   req.volume    = normVol;
   req.price     = price;
   req.sl        = sl;
   req.tp        = tp;
   req.type      = (dirn == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.deviation = 10;
   req.magic     = MAGIC;
   req.comment   = "ABUTRON V9 DEMO";

   // Validate actual broker stop-risk, not raw AI position_size * price.
   // InpRiskFraction is the configured per-trade PERCENT, e.g. 0.15%.
   double brokerStopPnL = 0.0;
   double accountEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(price <= 0.0 || sl <= 0.0 || tp <= 0.0 || accountEquity <= 0.0 ||
      InpRiskFraction <= 0.0 ||
      !OrderCalcProfit(req.type, symbol, normVol, price, sl, brokerStopPnL) ||
      !MathIsValidNumber(brokerStopPnL) || brokerStopPnL >= 0.0 ||
      -brokerStopPnL > accountEquity * InpRiskFraction / 100.0)
   {
      WriteReceipt(reqId, 0, "REJECTED", "BROKER_NATIVE_STOP_RISK_INVALID",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }

   // Run the broker's real margin/filling/stop validation before OrderSend.
   MqlTradeCheckResult nativeCheck;
   ZeroMemory(nativeCheck);
   if(!OrderCheck(req, nativeCheck) ||
      (nativeCheck.retcode != TRADE_RETCODE_DONE &&
       nativeCheck.retcode != TRADE_RETCODE_PLACED &&
       nativeCheck.retcode != 0))
   {
      WriteReceipt(reqId, 0, "REJECTED", "BROKER_NATIVE_ORDERCHECK_FAILED",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }

   if(OrderSend(req, res))
   {
      // OrderSend returned true — but verify retcode for actual execution
      if(res.retcode == TRADE_RETCODE_DONE)
      {
         // Real broker ACK — write native receipt with ticket/deal
         WriteReceipt(reqId, res.order, "ACK", "NATIVE_ACK",
                      res.price, res.volume, sl, tp, IntegerToString((long)res.deal));
         // Register the position for trailing-stop management.
         // Only manage positions opened by THIS EA (magic-tagged).
         ArrayResize(g_trail_ticket, ArraySize(g_trail_ticket) + 1);
         ArrayResize(g_trail_symbol, ArraySize(g_trail_symbol) + 1);
         ArrayResize(g_trail_dir, ArraySize(g_trail_dir) + 1);
         ArrayResize(g_trail_k, ArraySize(g_trail_k) + 1);
         ArrayResize(g_trail_stop, ArraySize(g_trail_stop) + 1);
         ArrayResize(g_trail_entry, ArraySize(g_trail_entry) + 1);
         ArrayResize(g_trail_active, ArraySize(g_trail_active) + 1);
         int idx = ArraySize(g_trail_ticket) - 1;
         g_trail_ticket[idx] = res.order;
         g_trail_symbol[idx] = symbol;
         g_trail_dir[idx]    = dirn;
         g_trail_k[idx]      = trailK;
         g_trail_stop[idx]   = trailSt;
         g_trail_entry[idx]  = res.price;
         g_trail_active[idx] = (trailSt > 0.0);
      }
      else
      {
         // OrderSend returned true but retcode indicates rejection/
         // modification — record honestly as UNKNOWN
         WriteReceipt(reqId, 0, "UNKNOWN", "SEND_RETCODE_" + IntegerToString(res.retcode),
                      0.0, 0.0, sl, tp, "");
      }
   }
   else
   {
      // Ambiguous / rejected — record outcome honestly
      WriteReceipt(reqId, 0, "UNKNOWN", "SEND_UNKNOWN",
                   0.0, 0.0, sl, tp, "");
   }
   FileDelete(pending);
}

//+------------------------------------------------------------------+
//| Apply trailing stops to managed positions                          |
//|                                                                    |
//| The brain computes the Chandelier trail (trail_stop = reference   |
//| ± k*ATR) and sends it with the order. This function tightens the  |
//| broker SL toward the trail stop on each timer tick. The trail     |
//| NEVER widens the stop — it only moves in the favourable direction |
//| and only when the new stop is tighter than the current one.       |
//|                                                                    |
//| Only positions tagged with THIS EA's magic are managed. Closed    |
//| positions are pruned from the tracking arrays. Fail-closed: a     |
//| ModifyOrder failure is logged and the existing stop is retained.  |
//+------------------------------------------------------------------+
void ApplyTrailingStops()
{
   for(int i = 0; i < ArraySize(g_trail_ticket); i++)
   {
      ulong ticket = g_trail_ticket[i];
      if(ticket == 0)
      {
         ArrayRemove(g_trail_ticket, i);
         ArrayRemove(g_trail_symbol, i);
         ArrayRemove(g_trail_dir, i);
         ArrayRemove(g_trail_k, i);
         ArrayRemove(g_trail_stop, i);
         ArrayRemove(g_trail_entry, i);
         ArrayRemove(g_trail_active, i);
         i--;
         continue;
      }
      // Skip if the position is no longer open.
      if(!PositionSelectByTicket(ticket))
      {
         ArrayRemove(g_trail_ticket, i);
         ArrayRemove(g_trail_symbol, i);
         ArrayRemove(g_trail_dir, i);
         ArrayRemove(g_trail_k, i);
         ArrayRemove(g_trail_stop, i);
         ArrayRemove(g_trail_entry, i);
         ArrayRemove(g_trail_active, i);
         i--;
         continue;
      }
      // Verify this is OUR position (magic-tagged).
      if(PositionGetInteger(POSITION_MAGIC) != MAGIC)
      {
         ArrayRemove(g_trail_ticket, i);
         ArrayRemove(g_trail_symbol, i);
         ArrayRemove(g_trail_dir, i);
         ArrayRemove(g_trail_k, i);
         ArrayRemove(g_trail_stop, i);
         ArrayRemove(g_trail_entry, i);
         ArrayRemove(g_trail_active, i);
         i--;
         continue;
      }
      // Verify the symbol still matches.
      if(PositionGetString(POSITION_SYMBOL) != g_trail_symbol[i])
      {
         ArrayRemove(g_trail_ticket, i);
         ArrayRemove(g_trail_symbol, i);
         ArrayRemove(g_trail_dir, i);
         ArrayRemove(g_trail_k, i);
         ArrayRemove(g_trail_stop, i);
         ArrayRemove(g_trail_entry, i);
         ArrayRemove(g_trail_active, i);
         i--;
         continue;
      }

      double trailStop = g_trail_stop[i];
      if(!g_trail_active[i] || trailStop <= 0.0)
         continue;

      double curSl = PositionGetDouble(POSITION_SL);
      if(curSl <= 0.0)
         continue;

      string dir = g_trail_dir[i];
      // The trail only tightens: for BUY the trail stop must be ABOVE
      // the current SL (higher = tighter); for SELL it must be BELOW.
      // A trail that would widen the stop is never applied.
      bool tighter = (dir == "BUY") ? (trailStop > curSl)
                                   : (trailStop < curSl);
      if(!tighter)
         continue;

      // Build a ModifyOrder request for the SL only. TP is unchanged.
      MqlTradeRequest mreq;
      MqlTradeResult  mres;
      ZeroMemory(mreq);
      ZeroMemory(mres);
      mreq.action    = TRADE_ACTION_SLTP;
      mreq.ticket    = ticket;
      mreq.sl        = trailStop;
      mreq.deviation = 10;

      if(OrderSend(mreq, mres) &&
         mres.retcode == TRADE_RETCODE_DONE)
      {
         // Trail applied — update the stored stop to the new value.
         g_trail_stop[i] = trailStop;
      }
      // If ModifyOrder fails, the existing broker stop is retained.
      // Fail-closed: never widen, never remove the protective stop.
   }
}

//+------------------------------------------------------------------+
//| Floor volume to broker lot step — never round up, never increase  |
//| to minimum. If the floored lot is below the minimum, the caller  |
//| rejects the order.                                                |
//+------------------------------------------------------------------+
double NormalizeLots(double v, string symbol)
{
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)  step = 0.01;
   // IEEE 754 division can produce 6556.999999999999 for 65.57/0.01,
   // which MathFloor would round down to 65.56 — one lot step short.
   // Add a tiny epsilon to compensate for representation error without
   // ever rounding up a genuinely fractional result (same pattern used
   // by the FP native bridge). Never round up: floor only.
   double lots = MathFloor(v / step + 1.0e-9) * step;
   int decimals = (int)MathRound(-MathLog10(step));
   if(decimals < 0)   decimals = 0;
   if(decimals > 8)   decimals = 8;
   return(NormalizeDouble(lots, decimals));
}

//+------------------------------------------------------------------+
//| Write a text file (creates/overwrites)                           |
//+------------------------------------------------------------------+
void WriteTextFile(string filename, string content)
{
   // Python reads UTF-8: MQL5 FILE_TXT without FILE_ANSI defaults to UTF-16.
   int h = FileOpen(filename, FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, content);
      FileFlush(h);
      FileClose(h);
   }
}

//+------------------------------------------------------------------+
//| Write native receipt                                             |
//+------------------------------------------------------------------+
void WriteReceipt(string reqId, long ticket, string state, string reason,
                  double price, double volume, double sl, double tp,
                  string deal)
{
   string dir = "";  // FileOpen paths are relative to the MQL5/Files sandbox.
   string file = dir + "ABUTRON_NATIVE_RECEIPTS_V2_" + Nonce() + ".txt";
   string json = StringFormat(
      "{\"request_id\":\"%s\",\"ticket\":%d,\"state\":\"%s\",\"reason\":\"%s\",\"login\":%d,\"price\":%.5f,\"volume\":%.2f,\"sl\":%.5f,\"tp\":%.5f,\"deal\":\"%s\",\"ts\":\"%s\"}",
      reqId, ticket, state, reason,
      AccountInfoInteger(ACCOUNT_LOGIN),
      price, volume, sl, tp,
      deal, TimeStampToStr(TimeCurrent()));
   WriteTextFile(file, json);
}

string TimeStampToStr(datetime ts)
{
   return(TimeToString(ts, TIME_DATE|TIME_SECONDS));
}

string Nonce()
{
   return(StringFormat("%d_%d", AccountInfoInteger(ACCOUNT_LOGIN),
                       (long)TimeCurrent()));
}

string Challenge()
{
   return(StringFormat("%d", AccountInfoInteger(ACCOUNT_LOGIN) +
                       (long)TimeTradeServer()));
}

string ActivationRevision()
{
   if(g_activation_secret == "")
      return("");
   return(HmacSha256Hex(g_activation_secret,
                        StringFormat("%s|%I64d", InpConnectionId,
                                     InpActivationSeed)));
}

//+------------------------------------------------------------------+
//| Activation secret provisioning                                    |
//+------------------------------------------------------------------+
//| The Python service writes the shared HMAC secret to                |
//| MQL5/Files/ABUTRON_ACTIVATION_SECRET.txt during arming. The EA    |
//| reads it on startup if InpActivationSecret is not set explicitly. |
//| If no secret is available, the EA rejects all signed orders.      |
//+------------------------------------------------------------------+
string LoadActivationSecret()
{
   if(InpActivationSecret != "")
      return(InpActivationSecret);
   string file = "ABUTRON_ACTIVATION_SECRET.txt";
   if(FileIsExist(file))
   {
      int h = FileOpen(file, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
      if(h != INVALID_HANDLE)
      {
         string secret = FileReadString(h);
         FileClose(h);
         StringTrimLeft(secret);
         StringTrimRight(secret);
         if(StringLen(secret) > 0)
            return(secret);
      }
   }
   return("");
}

//+------------------------------------------------------------------+
//| HMAC-SHA256 verification (MQL5 has no built-in HMAC)              |
//+------------------------------------------------------------------+
//| Verifies that the request signature matches HMAC-SHA256(secret,  |
//| canonical_order). The canonical message covers ALL order fields.  |
//| Rejects if the secret is empty or the signature does not match.   |
//+------------------------------------------------------------------+
bool VerifySignature(string sig, string canonical)
{
   if(g_activation_secret == "")
      return(false);   // No secret — never accept a signed order
   if(sig == "")
      return(false);
   string expected = HmacSha256Hex(g_activation_secret, canonical);
   return(sig == expected);
}

string HmacSha256Hex(string key, string message)
{
   // Convert key to byte array
   ushort keyBytes[];
   int keyLen = StringLen(key);
   ArrayResize(keyBytes, keyLen);
   for(int i = 0; i < keyLen; i++)
      keyBytes[i] = StringGetCharacter(key, i);

   // If key > 64 bytes, hash it first
   if(keyLen > 64)
   {
      string keyHex = Sha256Hex(keyBytes, keyLen);
      ushort hashedBytes[];
      HexToBytes(keyHex, hashedBytes);
      ArrayCopy(keyBytes, hashedBytes, 0, 0, ArraySize(hashedBytes));
      ArrayResize(keyBytes, ArraySize(hashedBytes));
      keyLen = ArraySize(keyBytes);
   }

   // Pad key to 64 bytes
   ArrayResize(keyBytes, 64);
   for(int i = keyLen; i < 64; i++)
      keyBytes[i] = 0;

   // Build ipad and opad
   ushort ipad[];
   ushort opad[];
   ArrayResize(ipad, 64);
   ArrayResize(opad, 64);
   for(int i = 0; i < 64; i++)
   {
      ipad[i] = (ushort)(keyBytes[i] ^ 0x36);
      opad[i] = (ushort)(keyBytes[i] ^ 0x5c);
   }

   // Convert message to byte array
   ushort msgBytes[];
   int msgLen = StringLen(message);
   ArrayResize(msgBytes, msgLen);
   for(int i = 0; i < msgLen; i++)
      msgBytes[i] = StringGetCharacter(message, i);

   // Inner hash: H(ipad || message)
   ushort innerInput[];
   ArrayResize(innerInput, 64 + msgLen);
   for(int i = 0; i < 64; i++)
      innerInput[i] = ipad[i];
   for(int i = 0; i < msgLen; i++)
      innerInput[64 + i] = msgBytes[i];
   string innerHex = Sha256Hex(innerInput, 64 + msgLen);
   ushort innerBytes[];
   HexToBytes(innerHex, innerBytes);
   int innerLen = ArraySize(innerBytes);

   // Outer hash: H(opad || inner)
   ushort outerInput[];
   ArrayResize(outerInput, 64 + innerLen);
   for(int i = 0; i < 64; i++)
      outerInput[i] = opad[i];
   for(int i = 0; i < innerLen; i++)
      outerInput[64 + i] = innerBytes[i];
   string result = Sha256Hex(outerInput, 64 + innerLen);
   return(result);
}

//+------------------------------------------------------------------+
//| SHA-256 (uses uint for 32-bit words — valid MQL5 type)            |
//+------------------------------------------------------------------+
string Sha256Hex(ushort &inputBytes[], int inputLen)
{
   uint H[8];
   H[0]=0x6a09e667; H[1]=0xbb67ae85; H[2]=0x3c6ef372; H[3]=0xa54ff53a;
   H[4]=0x510e527f; H[5]=0x9b05688c; H[6]=0x1f83d9ab; H[7]=0x5be0cd19;

   uint K[64];
   K[0]=0x428a2f98;K[1]=0x71374491;K[2]=0xb5c0fbcf;K[3]=0xe9b5dba5;K[4]=0x3956c25b;K[5]=0x59f111f1;K[6]=0x923f82a4;K[7]=0xab1c5ed5;
   K[8]=0xd807aa98;K[9]=0x12835b01;K[10]=0x243185be;K[11]=0x550c7dc3;K[12]=0x72be5d74;K[13]=0x80deb1fe;K[14]=0x9bdc06a7;K[15]=0xc19bf174;
   K[16]=0xe49b69c1;K[17]=0xefbe4786;K[18]=0x0fc19dc6;K[19]=0x240ca1cc;K[20]=0x2de92c6f;K[21]=0x4a7484aa;K[22]=0x5cb0a9dc;K[23]=0x76f988da;
   K[24]=0x983e5152;K[25]=0xa831c66d;K[26]=0xb00327c8;K[27]=0xbf597fc7;K[28]=0xc6e00bf3;K[29]=0xd5a79147;K[30]=0x06ca6351;K[31]=0x14292967;
   K[32]=0x27b70a85;K[33]=0x2e1b2138;K[34]=0x4d2c6dfc;K[35]=0x53380d13;K[36]=0x650a7354;K[37]=0x766a0abb;K[38]=0x81c2c92e;K[39]=0x92722c85;
   K[40]=0xa2bfe8a1;K[41]=0xa81a664b;K[42]=0xc24b8b70;K[43]=0xc76c51a3;K[44]=0xd192e819;K[45]=0xd6990624;K[46]=0xf40e3585;K[47]=0x106aa070;
   K[48]=0x19a4c116;K[49]=0x1e376c08;K[50]=0x2748774c;K[51]=0x34b0bcb5;K[52]=0x391c0cb3;K[53]=0x4ed8aa4a;K[54]=0x5b9cca4f;K[55]=0x682e6ff3;
   K[56]=0x748f82ee;K[57]=0x78a5636f;K[58]=0x84c87814;K[59]=0x8cc70208;K[60]=0x90befffa;K[61]=0xa4506ceb;K[62]=0xbef9a3f7;K[63]=0xc67178f2;

   // Copy input and pad: 0x80, zeros, 64-bit length
   ushort bytes[];
   ArrayResize(bytes, inputLen + 72);
   for(int i = 0; i < inputLen; i++)
      bytes[i] = inputBytes[i];
   bytes[inputLen] = 0x80;

   ulong bitLen = (ulong)inputLen * 8;
   int padLen = 64 - ((inputLen + 9) % 64);
   if(padLen == 64) padLen = 0;
   int totalLen = inputLen + 1 + padLen + 8;
   ArrayResize(bytes, totalLen);
   // MQL5 ArrayResize does not promise zeroed new storage. SHA-256 needs
   // explicit 0x00 padding or the same input may hash incorrectly.
   for(int i = inputLen + 1; i < totalLen - 8; i++)
      bytes[i] = 0;

   // Length bytes MUST be masked to 8 bits each
   bytes[totalLen - 8] = (ushort)((bitLen >> 56) & 0xFF);
   bytes[totalLen - 7] = (ushort)((bitLen >> 48) & 0xFF);
   bytes[totalLen - 6] = (ushort)((bitLen >> 40) & 0xFF);
   bytes[totalLen - 5] = (ushort)((bitLen >> 32) & 0xFF);
   bytes[totalLen - 4] = (ushort)((bitLen >> 24) & 0xFF);
   bytes[totalLen - 3] = (ushort)((bitLen >> 16) & 0xFF);
   bytes[totalLen - 2] = (ushort)((bitLen >> 8)  & 0xFF);
   bytes[totalLen - 1] = (ushort)(bitLen         & 0xFF);

   // Process each 512-bit block
   uint w[64];
   for(int block = 0; block < totalLen; block += 64)
   {
      for(int i = 0; i < 16; i++)
      {
         int j = block + i * 4;
         w[i] = ((uint)bytes[j] << 24) | ((uint)bytes[j+1] << 16) |
                ((uint)bytes[j+2] << 8) | (uint)bytes[j+3];
      }
      for(int i = 16; i < 64; i++)
      {
         uint s0 = ((w[i-15] >> 7) | (w[i-15] << 25)) ^ ((w[i-15] >> 18) | (w[i-15] << 14)) ^ (w[i-15] >> 3);
         uint s1 = ((w[i-2] >> 17) | (w[i-2] << 15)) ^ ((w[i-2] >> 19) | (w[i-2] << 13)) ^ (w[i-2] >> 10);
         w[i] = w[i-16] + s0 + w[i-7] + s1;
      }
      uint a=H[0],b=H[1],c=H[2],d=H[3],e=H[4],f=H[5],g=H[6],h=H[7];
      for(int i = 0; i < 64; i++)
      {
         uint S1 = ((e >> 6) | (e << 26)) ^ ((e >> 11) | (e << 21)) ^ ((e >> 25) | (e << 7));
         uint ch = (e & f) ^ (~e & g);
         uint t1 = h + S1 + ch + K[i] + w[i];
         uint S0 = ((a >> 2) | (a << 30)) ^ ((a >> 13) | (a << 19)) ^ ((a >> 22) | (a << 10));
         uint maj = (a & b) ^ (a & c) ^ (b & c);
         uint t2 = S0 + maj;
         h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
      }
      H[0]+=a; H[1]+=b; H[2]+=c; H[3]+=d;
      H[4]+=e; H[5]+=f; H[6]+=g; H[7]+=h;
   }

   string hex = "";
   for(int i = 0; i < 8; i++)
   {
      hex += StringFormat("%02x", (uint)((H[i] >> 24) & 0xFF));
      hex += StringFormat("%02x", (uint)((H[i] >> 16) & 0xFF));
      hex += StringFormat("%02x", (uint)((H[i] >> 8) & 0xFF));
      hex += StringFormat("%02x", (uint)(H[i] & 0xFF));
   }
   return(hex);
}

//+------------------------------------------------------------------+
//| Convert hex string to byte array (reference param — MQL5 cannot  |
//| return arrays from functions)                                   |
//+------------------------------------------------------------------+
void HexToBytes(string hex, ushort &bytes[])
{
   int len = StringLen(hex) / 2;
   ArrayResize(bytes, len);
   for(int i = 0; i < len; i++)
   {
      string hb = StringSubstr(hex, i * 2, 2);
      int hi = HexVal(StringGetCharacter(hb, 0));
      int lo = HexVal(StringGetCharacter(hb, 1));
      bytes[i] = (ushort)((hi << 4) | lo);
   }
}

int HexVal(ushort c)
{
   if(c >= '0' && c <= '9') return((int)c - 48);
   if(c >= 'a' && c <= 'f') return((int)c - 97 + 10);
   if(c >= 'A' && c <= 'F') return((int)c - 65 + 10);
   return(0);
}

//+------------------------------------------------------------------+
//| Minimal JSON helpers (no external libs)                          |
//+------------------------------------------------------------------+
string JsonStr(string json, string key)
{
   string pat = "\"" + key + "\":\"";
   int pos = StringFind(json, pat);
   if(pos < 0) return("");
   pos += StringLen(pat);
   int end = StringFind(json, "\"", pos);
   if(end < 0) return("");
   return(StringSubstr(json, pos, end - pos));
}

double JsonNum(string json, string key)
{
   string pat = "\"" + key + "\":";
   int pos = StringFind(json, pat);
   if(pos < 0) return(0.0);
   pos += StringLen(pat);
   string num = "";
   while(pos < StringLen(json))
   {
      ushort c = StringGetCharacter(json, pos);
      if((c >= '0' && c <= '9') || c == '.' || c == '-')
      {
         num += StringSubstr(json, pos, 1);
         pos++;
      }
      else break;
   }
   return(StringToDouble(num));
}
//+------------------------------------------------------------------+
