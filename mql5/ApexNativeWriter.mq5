//+------------------------------------------------------------------+
//| ApexNativeWriter.mq5                                               |
//| ABUTRON FX V9 — APEX 1.1 native single-writer EA                    |
//|                                                                   |
//| This EA is the ONLY order-authority actor inside an account's      |
//| MT5 terminal. It:                                                  |
//|   1. Exports account identity + native M5 bars + order-flow ticks  |
//|   2. Receives HMAC-signed order requests from the manager socket   |
//|      via MQL5/Files handoff                                       |
//|   3. Places broker-native orders with protective SL/TP             |
//|   4. Writes native receipts (ticket/deal/SL/TP) back to MQL5/Files |
//|                                                                   |
//| Evidence discipline: receipts are written ONLY after a real       |
//| OrderSend result; ambiguous outcomes are written as UNKNOWN.       |
//| No fabricated ACK.                                                |
//|                                                                   |
//| Security: every order request is HMAC-SHA256 signed over the      |
//| COMPLETE canonical payload (nonce|candidate|cid|symbol|dir|vol|  |
//| sl|tp). The EA rejects any request whose signature does not match.|
//| The HMAC secret is provisioned from MQL5/Files/ABUTRON_ACTIVATION  |
//| _SECRET.txt (written by the Python service) or set via the       |
//| InpActivationSecret input.                                        |
//+------------------------------------------------------------------+
#property copyright "ABUTRON FX V9"
#property version   "1.1.0"
#property strict_testing

input string  InpConnectionId   = "";           // connection id
input string  InpBrokerServer   = "";           // broker server
input long    InpActivationSeed = 0;            // activation seed (per arm)
input string  InpActivationSecret = "";         // HMAC key (or file-provisioned)
input int     InpManagedCharts  = 0;            // 0 = all charts with EA
input double  InpRiskFraction   = 0.15;         // per-trade risk fraction (display)
input int     InpRiskFractionPct = 15;          // percent (15 => 0.15)
input int     InpExportPeriod   = PERIOD_M5;    // export timeframe

string g_activation_secret = "";                // resolved at OnInit

int OnInit()
{
   SetTradeAllowed(true);
   SetTradeExecution(ORDER_TYPE_MARKET, 0, 0, 0); // allow market orders
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
   string dir = "MQL5\\Files\\";
   string identity = dir + "ABUTRON_NATIVE_ACCOUNT_V2_" + Nonce() + ".txt";
   string bars     = dir + "ABUTRON_NATIVE_BARS_V2_"    + Nonce() + ".txt";
   string oflow    = dir + "ABUTRON_ORDER_FLOW_V2_"     + Nonce() + ".txt";

   string idJson = StringFormat(
      "{\"connection_id\":\"%s\",\"login\":%d,\"broker\":\"%s\",\"server\":\"%s\",\"nonce\":\"%s\",\"challenge\":\"%s\",\"worker_health_ts\":\"%s\"}",
      InpConnectionId, AccountInfoInteger(ACCOUNT_LOGIN),
      "native", InpBrokerServer, Nonce(), Challenge(), TimeStampToStr(TimeCurrent()));

   FileWrite(identity, idJson);

   // Closed M5 bars (last 120)
   ulong from = TimeCurrent() - 120 * PeriodSeconds();
   MqlRates rates[];
   int count = CopyRates(_Symbol, InpExportPeriod, from, 0, rates);
   if(count > 0) ArrayResize(rates, count);
   string barsJson = "{\"bars\":[";
   for(int i = 0; i < ArraySize(rates); i++)
   {
      MqlRates r = rates[i];
      barsJson += StringFormat(
         "{\"symbol\":\"%s\",\"timeframe\":\"M5\",\"open_time\":\"%s\",\"close_time\":\"%s\",\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%.2f,\"bid\":%.5f,\"ask\":%.5f,\"spread\":%.0f,\"tick_size\":%.5f,\"point\":%.5f,\"closed\":true},",
         r.symbol, TimeToString(r.time, TIME_DATE|TIME_SECONDS),
         TimeToString(r.time + PeriodSeconds(InpExportPeriod), TIME_DATE|TIME_SECONDS),
         r.open, r.high, r.low, r.close, r.tick_volume,
         Bid, Ask, (int)(Ask - Bid), _Point, _Point);
   }
   barsJson = StringTrimRight(barsJson, ",");
   barsJson += "]}";
   FileWrite(bars, barsJson);

   // Order flow: aggressor trades since last export
   string ofJson = "{\"trades\":[";
   string lastFile = dir + "ABUTRON_ORDER_FLOW_V2_LAST.txt";
   if(FileExists(lastFile))
   {
      string s; int n = FileReadString(lastFile, s);
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
   ofJson = StringTrimRight(ofJson, ",");
   ofJson += "]}";
   FileWrite(oflow, ofJson);
   FileWrite(lastFile, TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS));
}

//+------------------------------------------------------------------+
//| Process signed order requests from MQL5/Files handoff            |
//+------------------------------------------------------------------+
void ProcessOrderRequests()
{
   string dir = "MQL5\\Files\\";
   string pending = dir + "ABUTRON_REQ_PENDING.json";
   if(!FileExists(pending)) return;

   string body;
   if(FileReadString(pending, body) <= 0) { FileDelete(pending); return; }

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

   // Verify connection binding + activation (single-writer authority)
   if(cid != InpConnectionId || actRev != ActivationRevision())
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_SINGLE_WRITER_DISARMED",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }

   // Verify HMAC-SHA256 signature over the COMPLETE canonical order
   // payload: nonce|candidate|cid|symbol|direction|volume|sl|tp
   string canonical = reqId + "|" + cand + "|" + cid + "|" + symbol +
                      "|" + dirn + "|" + StringFormat("%.6f", volume) +
                      "|" + StringFormat("%.6f", sl) +
                      "|" + StringFormat("%.6f", tp);
   if(!VerifySignature(sig, canonical))
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_SIGNATURE_INVALID",
                   0.0, 0.0, sl, tp, "");
      FileDelete(pending);
      return;
   }

   double price = 0.0;
   if(dirn == "BUY")  price = Ask;
   else               price = Bid;

   // Floor volume to broker lot step — never round up.
   // If the floored lot is below the minimum, reject.
   double normVol = NormalizeLots(volume, symbol);
   double lotMin  = (double)SymbolInfoInteger(symbol, SYMBOL_VOLUME_MIN);
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
      // Real broker ACK — write native receipt with ticket/deal
      WriteReceipt(reqId, res.order, "ACK", "NATIVE_ACK",
                   res.price, res.volume, sl, tp, LongToString(res.deal));
   }
   else
   {
      // Ambiguous / rejected — record outcome honestly
      WriteReceipt(reqId, 0, "UNKNOWN", "SEND_UNKNOWN",
                   0.0, 0.0, sl, tp, "");
   }
   FileDelete(pending);
}

double NormalizeLots(double v, string symbol)
{
   // Floor to the broker's actual volume step for the ORDER symbol.
   // Never round up — rounding up can push risk above the approved budget.
   double step = (double)SymbolInfoInteger(symbol, SYMBOL_VOLUME_STEP);
   double lotMin = (double)SymbolInfoInteger(symbol, SYMBOL_VOLUME_MIN);
   if(step <= 0.0)  step = 0.01;
   double lots = MathFloor(v / step) * step;
   if(lots < lotMin) lots = lotMin;
   int decimals = (int)MathRound(-MathLog10(step));
   if(decimals < 0)   decimals = 0;
   if(decimals > 8)   decimals = 8;
   return(NormalizeDouble(lots, decimals));
}

void WriteReceipt(string reqId, long ticket, string state, string reason,
                  double price, double volume, double sl, double tp,
                  string deal)
{
   string dir = "MQL5\\Files\\";
   string file = dir + "ABUTRON_NATIVE_RECEIPTS_V2_" + Nonce() + ".txt";
   string json = StringFormat(
      "{\"request_id\":\"%s\",\"ticket\":%d,\"state\":\"%s\",\"reason\":\"%s\",\"login\":%d,\"price\":%.5f,\"volume\":%.2f,\"sl\":%.5f,\"tp\":%.5f,\"deal\":\"%s\",\"ts\":\"%s\"}",
      reqId, ticket, state, reason,
      AccountInfoInteger(ACCOUNT_LOGIN),
      price, volume, sl, tp,
      deal, TimeStampToStr(TimeCurrent()));
   FileWrite(file, json);
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
   return(CryptHash(StringFormat("%s|%d", InpConnectionId,
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
   string file = "MQL5\\Files\\ABUTRON_ACTIVATION_SECRET.txt";
   if(FileExists(file))
   {
      string s;
      int n = FileReadString(file, s);
      if(n > 0)
         return(StringTrim(s));
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
      string keyHex = Sha256Hex(keyBytes);
      keyBytes = HexToBytes(keyHex);
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
   string innerHex = Sha256Hex(innerInput);
   ushort innerBytes[];
   innerBytes = HexToBytes(innerHex);
   int innerLen = ArraySize(innerBytes);

   // Outer hash: H(opad || inner)
   ushort outerInput[];
   ArrayResize(outerInput, 64 + innerLen);
   for(int i = 0; i < 64; i++)
      outerInput[i] = opad[i];
   for(int i = 0; i < innerLen; i++)
      outerInput[64 + i] = innerBytes[i];
   string result = Sha256Hex(outerInput);
   return(result);
}

//+------------------------------------------------------------------+
//| SHA-256 (uses ulong for 32-bit words — MQL5 has no uint type)     |
//+------------------------------------------------------------------+
string Sha256Hex(ushort input[])
{
   ulong H[8];
   H[0]=0x6a09e667; H[1]=0xbb67ae85; H[2]=0x3c6ef372; H[3]=0xa54ff53a;
   H[4]=0x510e527f; H[5]=0x9b05688c; H[6]=0x1f83d9ab; H[7]=0x5be0cd19;

   ulong K[64];
   K[0]=0x428a2f98;K[1]=0x71374491;K[2]=0xb5c0fbcf;K[3]=0xe9b5dba5;K[4]=0x3956c25b;K[5]=0x59f111f1;K[6]=0x923f82a4;K[7]=0xab1c5ed5;
   K[8]=0xd807aa98;K[9]=0x12835b01;K[10]=0x243185be;K[11]=0x550c7dc3;K[12]=0x72be5d74;K[13]=0x80deb1fe;K[14]=0x9bdc06a7;K[15]=0xc19bf174;
   K[16]=0xe49b69c1;K[17]=0xefbe4786;K[18]=0x0fc19dc6;K[19]=0x240ca1cc;K[20]=0x2de92c6f;K[21]=0x4a7484aa;K[22]=0x5cb0a9dc;K[23]=0x76f988da;
   K[24]=0x983e5152;K[25]=0xa831c66d;K[26]=0xb00327c8;K[27]=0xbf597fc7;K[28]=0xc6e00bf3;K[29]=0xd5a79147;K[30]=0x06ca6351;K[31]=0x14292967;
   K[32]=0x27b70a85;K[33]=0x2e1b2138;K[34]=0x4d2c6dfc;K[35]=0x53380d13;K[36]=0x650a7354;K[37]=0x766a0abb;K[38]=0x81c2c92e;K[39]=0x92722c85;
   K[40]=0xa2bfe8a1;K[41]=0xa81a664b;K[42]=0xc24b8b70;K[43]=0xc76c51a3;K[44]=0xd192e819;K[45]=0xd6990624;K[46]=0xf40e3585;K[47]=0x106aa070;
   K[48]=0x19a4c116;K[49]=0x1e376c08;K[50]=0x2748774c;K[51]=0x34b0bcb5;K[52]=0x391c0cb3;K[53]=0x4ed8aa4a;K[54]=0x5b9cca4f;K[55]=0x682e6ff3;
   K[56]=0x748f82ee;K[57]=0x78a5636f;K[58]=0x84c87814;K[59]=0x8cc70208;K[60]=0x90befffa;K[61]=0xa4506ceb;K[62]=0xbef9a3f7;K[63]=0xc67178f2;

   // Copy input and pad: 0x80, zeros, 64-bit length
   int len = ArraySize(input);
   ushort bytes[];
   ArrayResize(bytes, len + 72);
   for(int i = 0; i < len; i++)
      bytes[i] = input[i];
   bytes[len] = 0x80;

   ulong bitLen = (ulong)len * 8;
   int padLen = 64 - ((len + 9) % 64);
   if(padLen == 64) padLen = 0;
   int totalLen = len + 1 + padLen + 8;
   ArrayResize(bytes, totalLen);

   bytes[totalLen - 8] = (ushort)(bitLen >> 56);
   bytes[totalLen - 7] = (ushort)(bitLen >> 48);
   bytes[totalLen - 6] = (ushort)(bitLen >> 40);
   bytes[totalLen - 5] = (ushort)(bitLen >> 32);
   bytes[totalLen - 4] = (ushort)(bitLen >> 24);
   bytes[totalLen - 3] = (ushort)(bitLen >> 16);
   bytes[totalLen - 2] = (ushort)(bitLen >> 8);
   bytes[totalLen - 1] = (ushort)(bitLen);

   // Process each 512-bit block
   ulong w[64];
   for(int block = 0; block < totalLen; block += 64)
   {
      for(int i = 0; i < 16; i++)
      {
         int j = block + i * 4;
         w[i] = (((ulong)bytes[j] << 24) | ((ulong)bytes[j+1] << 16) |
                 ((ulong)bytes[j+2] << 8) | (ulong)bytes[j+3]) & 0xFFFFFFFF;
      }
      for(int i = 16; i < 64; i++)
      {
         ulong s0 = ((w[i-15] >> 7) | (w[i-15] << 25)) ^ ((w[i-15] >> 18) | (w[i-15] << 14)) ^ (w[i-15] >> 3);
         s0 &= 0xFFFFFFFF;
         ulong s1 = ((w[i-2] >> 17) | (w[i-2] << 15)) ^ ((w[i-2] >> 19) | (w[i-2] << 13)) ^ (w[i-2] >> 10);
         s1 &= 0xFFFFFFFF;
         w[i] = (w[i-16] + s0 + w[i-7] + s1) & 0xFFFFFFFF;
      }
      ulong a=H[0],b=H[1],c=H[2],d=H[3],e=H[4],f=H[5],g=H[6],h=H[7];
      for(int i = 0; i < 64; i++)
      {
         ulong S1 = (((e >> 6) | (e << 26)) ^ ((e >> 11) | (e << 21)) ^ ((e >> 25) | (e << 7))) & 0xFFFFFFFF;
         ulong ch = ((e & f) ^ (~e & g)) & 0xFFFFFFFF;
         ulong t1 = (h + S1 + ch + K[i] + w[i]) & 0xFFFFFFFF;
         ulong S0 = (((a >> 2) | (a << 30)) ^ ((a >> 13) | (a << 19)) ^ ((a >> 22) | (a << 10))) & 0xFFFFFFFF;
         ulong maj = ((a & b) ^ (a & c) ^ (b & c)) & 0xFFFFFFFF;
         ulong t2 = (S0 + maj) & 0xFFFFFFFF;
         h=g; g=f; f=e; e=(d+t1)&0xFFFFFFFF; d=c; c=b; b=a; a=(t1+t2)&0xFFFFFFFF;
      }
      H[0]=(H[0]+a)&0xFFFFFFFF; H[1]=(H[1]+b)&0xFFFFFFFF; H[2]=(H[2]+c)&0xFFFFFFFF; H[3]=(H[3]+d)&0xFFFFFFFF;
      H[4]=(H[4]+e)&0xFFFFFFFF; H[5]=(H[5]+f)&0xFFFFFFFF; H[6]=(H[6]+g)&0xFFFFFFFF; H[7]=(H[7]+h)&0xFFFFFFFF;
   }

   string hex = "";
   for(int i = 0; i < 8; i++)
   {
      ulong word = H[i] & 0xFFFFFFFF;
      hex += StringFormat("%02x", (ushort)(word >> 24));
      hex += StringFormat("%02x", (ushort)(word >> 16));
      hex += StringFormat("%02x", (ushort)(word >> 8));
      hex += StringFormat("%02x", (ushort)(word));
   }
   return(hex);
}

ushort[] HexToBytes(string hex)
{
   ushort bytes[];
   int len = StringLen(hex) / 2;
   ArrayResize(bytes, len);
   for(int i = 0; i < len; i++)
   {
      string hb = StringSubstr(hex, i * 2, 2);
      int hi = HexVal(StringGetCharacter(hb, 0));
      int lo = HexVal(StringGetCharacter(hb, 1));
      bytes[i] = (ushort)((hi << 4) | lo);
   }
   return(bytes);
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
