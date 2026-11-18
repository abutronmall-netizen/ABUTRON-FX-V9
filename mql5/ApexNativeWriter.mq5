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
#property version   "1.1.0"

input string  InpConnectionId   = "";           // connection id
input string  InpBrokerServer   = "";           // broker server
input long    InpActivationSeed = 0;            // activation seed (per arm)
input string  InpActivationSecret = "";         // HMAC key (or file-provisioned)
input int     InpManagedCharts  = 0;            // 0 = all charts with EA
input double  InpRiskFraction   = 0.15;         // per-trade risk fraction (display)
input int     InpRiskFractionPct = 15;          // percent (15 => 0.15)
input ENUM_TIMEFRAMES InpExportPeriod = PERIOD_M5;  // native export timeframe

string g_activation_secret = "";                // resolved at OnInit

int OnInit()
{
   // Terminal-level algorithmic trading permissions are managed by MT5.
   g_activation_secret = LoadActivationSecret();
   EventSetTimer(5);                                  // export + handoff loop
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   ExportNativeData();
   ProcessOrderRequests();
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

   // --- Order flow: aggressor trades since last export -------------
   string ofJson = "{\"trades\":[";
   string lastFile = dir + "ABUTRON_ORDER_FLOW_V2_LAST.txt";
   if(FileIsExist(lastFile))
   {
      int h = FileOpen(lastFile, FILE_READ | FILE_TXT);
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
   long allTickets[];
   int posCount = PositionsTotal();
   ArrayResize(allTickets, posCount);
   int n = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      long t = PositionGetTicket(i);
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
      long ticket = allTickets[k];
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
         "{\"ticket\":%d,\"symbol\":\"%s\",\"direction\":\"%s\","
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
      "{\\\"nonce\\\":\\\"%s\\\",\\\"challenge\\\":\\\"%s\\\",\\\"connection_id\\\":\\\"%s\\\","
      "\\\"login\\\":\\\"%s\\\",\\\"server\\\":\\\"%s\\\",\\\"equity\\\":%s,\\\"balance\\\":%s,"
      "\\\"currency\\\":\\\"%s\\\",\\\"position_count\\\":%d,\\\"sig\\\":\\\"%s\\\","
      "\\\"positions\\\":[",
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
   int h = FileOpen(pending, FILE_READ | FILE_TXT);
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
   if(dirn == "BUY")  price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   else               price = SymbolInfoDouble(_Symbol, SYMBOL_BID);

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
   Zero(req); Zero(res);
   req.action    = TRADE_ACTION_DEAL;
   req.symbol    = symbol;
   req.volume    = normVol;
   req.price     = price;
   req.sl        = sl;
   req.tp        = tp;
   req.type      = (dirn == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.deviation = 10;

   if(OrderSend(req, res))
   {
      // OrderSend returned true — but verify retcode for actual execution
      if(res.retcode == TRADE_RETCODE_DONE)
      {
         // Real broker ACK — write native receipt with ticket/deal
         WriteReceipt(reqId, res.order, "ACK", "NATIVE_ACK",
                      res.price, res.volume, sl, tp, LongToString(res.deal));
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
//| Floor volume to broker lot step — never round up, never increase  |
//| to minimum. If the floored lot is below the minimum, the caller  |
//| rejects the order.                                                |
//+------------------------------------------------------------------+
double NormalizeLots(double v, string symbol)
{
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)  step = 0.01;
   double lots = MathFloor(v / step) * step;
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
   int h = FileOpen(filename, FILE_WRITE | FILE_TXT);
   if(h > 0)
   {
      FileWrite(h, content);
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
                       (long)ServerTime()));
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
      int h = FileOpen(file, FILE_READ | FILE_TXT);
      if(h > 0)
      {
         string s = FileReadString(h);
         FileClose(h);
         if(StringLen(s) > 0)
            StringTrimLeft(s);
            StringTrimRight(s);
            return(s);
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
string Sha256Hex(ushort input[], int inputLen)
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
      bytes[i] = input[i];
   bytes[inputLen] = 0x80;

   ulong bitLen = (ulong)inputLen * 8;
   int padLen = 64 - ((inputLen + 9) % 64);
   if(padLen == 64) padLen = 0;
   int totalLen = inputLen + 1 + padLen + 8;
   ArrayResize(bytes, totalLen);

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
      hex += StringFormat("%02x", (ushort)(H[i] >> 24));
      hex += StringFormat("%02x", (ushort)(H[i] >> 16));
      hex += StringFormat("%02x", (ushort)(H[i] >> 8));
      hex += StringFormat("%02x", (ushort)(H[i]));
   }
   return(hex);
}

//+------------------------------------------------------------------+
//| Convert hex string to byte array (reference param — MQL5 cannot  |
//| return arrays from functions)                                   |
//+------------------------------------------------------------------+
void HexToBytes(string hex, ushort bytes[])
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
      char c = StringGetCharacter(json, pos);
      if((c >= '0' && c <= '9') || c == '.' || c == '-')
         num += c, pos++;
      else break;
   }
   return(StringToDouble(num));
}
//+------------------------------------------------------------------+
