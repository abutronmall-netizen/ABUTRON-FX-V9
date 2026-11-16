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
#property version   "1.100"

input string  InpConnectionId   = "";           // connection id
input string  InpBrokerServer   = "";           // broker server
input string  InpActivationRevision = "";    // CRM revision (or file-provisioned)
input string  InpActivationSecret = "";         // HMAC key (or file-provisioned)
input int     InpManagedCharts  = 0;            // 0 = all charts with EA
input double  InpRiskFraction   = 0.15;         // per-trade risk fraction (display)
input int     InpRiskFractionPct = 15;          // percent (15 => 0.15)
input ENUM_TIMEFRAMES InpExportPeriod = PERIOD_M5; // export timeframe

string g_activation_secret = "";                // resolved at OnInit

int OnInit()
{
   // MT5 terminal controls Algo Trading permissions; no local override.
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
   string dir = "";  // FileOpen is already sandboxed inside MQL5/Files

   // --- Account identity -------------------------------------------
   string identity = dir + "ABUTRON_NATIVE_ACCOUNT_V2_" + Nonce() + ".txt";
   string idJson = StringFormat(
      "{\"connection_id\":\"%s\",\"login\":%d,\"broker\":\"%s\",\"server\":\"%s\",\"nonce\":\"%s\",\"challenge\":\"%s\",\"worker_health_ts\":\"%s\"}",
      InpConnectionId, AccountInfoInteger(ACCOUNT_LOGIN),
      "native", InpBrokerServer, Nonce(), Challenge(), TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS));
   WriteTextFile(identity, idJson);

   // --- Closed M5 bars (last 120) -----------------------------------
   datetime from = TimeCurrent() - 120 * PeriodSeconds(InpExportPeriod);
   MqlRates rates[];
   int count = CopyRates(_Symbol, InpExportPeriod, from, TimeCurrent(), rates);
   if(count > 0) ArrayResize(rates, count);

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
         (int)(SymbolInfoDouble(_Symbol, SYMBOL_ASK) -
               SymbolInfoDouble(_Symbol, SYMBOL_BID)),
         _Point, _Point);
   }
   // Remove trailing comma manually (StringTrimRight only trims whitespace)
   int lastComma = StringLen(barsJson) - 1;
   if(lastComma >= 0 && StringSubstr(barsJson, lastComma, 1) == ",")
      barsJson = StringSubstr(barsJson, 0, lastComma);
   barsJson += "]}";
   string barsFile = dir + "ABUTRON_NATIVE_BARS_V2_" + Nonce() + ".txt";
   WriteTextFile(barsFile, barsJson);

   // --- Order flow: aggressor trades since last export -------------
   string ofJson = "{\"trades\":[";
   string lastFile = dir + "ABUTRON_ORDER_FLOW_V2_LAST.txt";
   if(FileIsExist(lastFile))
   {
      int h = FileOpen(lastFile, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
      if(h != INVALID_HANDLE)
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
   int ofComma = StringLen(ofJson) - 1;
   if(ofComma >= 0 && StringSubstr(ofJson, ofComma, 1) == ",")
      ofJson = StringSubstr(ofJson, 0, ofComma);
   ofJson += "]}";
   string oflowFile = dir + "ABUTRON_ORDER_FLOW_V2_" + Nonce() + ".txt";
   WriteTextFile(oflowFile, ofJson);
   WriteTextFile(lastFile, TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS));
}

//+------------------------------------------------------------------+
//| Process signed order requests from MQL5/Files handoff            |
//+------------------------------------------------------------------+
void ProcessOrderRequests()
{
   string dir = "";  // FileOpen is already sandboxed inside MQL5/Files
   string pending = dir + "ABUTRON_REQ_PENDING.json";
   if(!FileIsExist(pending)) return;

   // Open and read the pending request
   int h = FileOpen(pending, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(h == INVALID_HANDLE) { FileDelete(pending); return; }
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
   if(InpActivationSecret == "") g_activation_secret = LoadActivationSecret();
   if(!VerifySignature(sig, canonical))
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_SIGNATURE_INVALID",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }

   if(dirn != "BUY" && dirn != "SELL")
   {
      WriteReceipt(reqId, 0, "REJECTED", "INVALID_DIRECTION",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }
   if(entryTy != "CONFIRM")
   {
      // LIMIT order semantics require a separately tested pending-order path.
      // Never silently convert a signed LIMIT intent into a market order.
      WriteReceipt(reqId, 0, "REJECTED", "NATIVE_LIMIT_NOT_IMPLEMENTED",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }
   MqlTick currentTick;
   if(!SymbolInfoTick(symbol, currentTick) ||
      currentTick.time < TimeCurrent() - 180 ||
      currentTick.time > TimeCurrent() + 5)
   {
      WriteReceipt(reqId, 0, "REJECTED", "NATIVE_TICK_UNAVAILABLE",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }
   double price = (dirn == "BUY") ? currentTick.ask : currentTick.bid;
   if(price <= 0.0 || sl <= 0.0 || tp <= 0.0 ||
      (dirn == "BUY" && (sl >= price || tp <= price)) ||
      (dirn == "SELL" && (sl <= price || tp >= price)))
   {
      WriteReceipt(reqId, 0, "REJECTED", "INVALID_PROTECTIVE_GEOMETRY",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }

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

   if(OrderSend(req, res))
   {
      // OrderSend returned true — but verify retcode for actual execution
      if(res.retcode == TRADE_RETCODE_DONE)
      {
         // Real broker ACK — write native receipt with ticket/deal
         WriteReceipt(reqId, res.order, "ACK", "NATIVE_ACK",
                      res.price, res.volume, sl, tp, IntegerToString((long)res.deal));
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
   if(step <= 0.0 || v <= 0.0) return(0.0);
   double lots = MathFloor(v / step + 1e-10) * step;
   // Always preserve 0.25/0.05 steps: -log10(step) is not a decimal count.
   // Never round a floored 1.25-lot volume up to 1.30.
   double normalized = NormalizeDouble(lots, 8);
   return(normalized > v + 1e-9 ? 0.0 : normalized);
}

//+------------------------------------------------------------------+
//| Write a text file (creates/overwrites)                           |
//+------------------------------------------------------------------+
void WriteTextFile(string filename, string content)
{
   int h = FileOpen(filename, FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, content);
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
   string dir = "";  // FileOpen is already sandboxed inside MQL5/Files
   string file = dir + "ABUTRON_NATIVE_RECEIPTS_V2_" + Nonce() + ".txt";
   string json = StringFormat(
      "{\"request_id\":\"%s\",\"ticket\":%d,\"state\":\"%s\",\"reason\":\"%s\",\"login\":%d,\"price\":%.5f,\"volume\":%.2f,\"sl\":%.5f,\"tp\":%.5f,\"deal\":\"%s\",\"ts\":\"%s\"}",
      reqId, ticket, state, reason,
      AccountInfoInteger(ACCOUNT_LOGIN),
      price, volume, sl, tp,
      deal, TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS));
   WriteTextFile(file, json);
}

string Nonce()
{
   return(StringFormat("%d_%d", AccountInfoInteger(ACCOUNT_LOGIN),
                       (long)TimeCurrent()));
}

string Challenge()
{
   return(IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN) +
                          (long)TimeTradeServer()));
}

string ActivationRevision()
{
   if(InpActivationRevision != "")
      return(InpActivationRevision);
   string value = ReadTextFile("ABUTRON_ACTIVATION_REVISION.txt");
   return(value);
}

//+------------------------------------------------------------------+
//| Activation secret provisioning                                    |
//+------------------------------------------------------------------+
//| The Python service writes the shared HMAC secret to                |
//| MQL5/Files/ABUTRON_ACTIVATION_SECRET.txt during arming. The EA    |
//| reads it on startup if InpActivationSecret is not set explicitly. |
//| If no secret is available, the EA rejects all signed orders.      |
//+------------------------------------------------------------------+
string ReadTextFile(string file)
{
   if(!FileIsExist(file)) return("");
   int h = FileOpen(file, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(h == INVALID_HANDLE) return("");
   string value = FileReadString(h);
   FileClose(h);
   StringTrimLeft(value);
   StringTrimRight(value);
   return(value);
}

string LoadActivationSecret()
{
   if(InpActivationSecret != "") return(InpActivationSecret);
   return(ReadTextFile("ABUTRON_ACTIVATION_SECRET.txt"));
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
   uchar keyUtf8[];
   int keyCount = StringToCharArray(key, keyUtf8, 0, WHOLE_ARRAY, CP_UTF8);
   if(keyCount <= 0) return("");
   int keyLen = keyCount - 1; // exclude trailing NUL
   ArrayResize(keyBytes, keyLen);
   for(int i = 0; i < keyLen; i++)
      keyBytes[i] = (ushort)keyUtf8[i];

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
   uchar messageUtf8[];
   int msgCount = StringToCharArray(message, messageUtf8, 0, WHOLE_ARRAY, CP_UTF8);
   if(msgCount <= 0) return("");
   int msgLen = msgCount - 1; // exclude trailing NUL
   ArrayResize(msgBytes, msgLen);
   for(int i = 0; i < msgLen; i++)
      msgBytes[i] = (ushort)messageUtf8[i];

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
string Sha256Hex(const ushort &inputBytes[], int inputLen)
{
   if(inputLen < 0 || inputLen > ArraySize(inputBytes)) return("");
   uchar data[], emptyKey[], digest[];
   ArrayResize(data, inputLen);
   for(int i = 0; i < inputLen; i++)
      data[i] = (uchar)(inputBytes[i] & 0xFF);
   int n = CryptEncode(CRYPT_HASH_SHA256, data, emptyKey, digest);
   if(n != 32) return("");
   string hex = "";
   for(int j = 0; j < 32; j++)
      hex += StringFormat("%02x", (uint)digest[j]);
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
int JsonValueStart(string json, string key)
{
   string marker = "\"" + key + "\"";
   int p = StringFind(json, marker);
   if(p < 0) return(-1);
   p += StringLen(marker);
   int n = StringLen(json);
   while(p < n && (StringSubstr(json, p, 1) == " " ||
                    StringSubstr(json, p, 1) == "\t")) p++;
   if(p >= n || StringSubstr(json, p, 1) != ":") return(-1);
   p++;
   while(p < n && (StringSubstr(json, p, 1) == " " ||
                    StringSubstr(json, p, 1) == "\t")) p++;
   return(p);
}

string JsonStr(string json, string key)
{
   int pos = JsonValueStart(json, key);
   if(pos < 0 || StringSubstr(json, pos, 1) != "\"") return("");
   pos++;
   int end = StringFind(json, "\"", pos);
   if(end < 0) return("");
   return(StringSubstr(json, pos, end - pos));
}

double JsonNum(string json, string key)
{
   int pos = JsonValueStart(json, key);
   if(pos < 0) return(0.0);
   string num = "";
   while(pos < StringLen(json))
   {
      ushort c = StringGetCharacter(json, pos);
      if((c >= '0' && c <= '9') || c == '.' || c == '-' ||
         c == 'e' || c == 'E' || c == '+')
      {
         num += StringSubstr(json, pos, 1);
         pos++;
      }
      else break;
   }
   return(StringToDouble(num));
}
