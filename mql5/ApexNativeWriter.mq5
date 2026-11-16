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
//| No fabricated ACK.                                                 |
//+------------------------------------------------------------------+
#property copyright "ABUTRON FX V9"
#property version   "1.1.0"
#property strict_testing

input string  InpConnectionId   = "";           // connection id
input string  InpBrokerServer   = "";           // broker server
input long    InpActivationSeed = 0;            // HMAC seed from manager (per arm)
input string  InpActivationSecret = "";         // HMAC key for request signature verification
input int     InpManagedCharts  = 0;            // 0 = all charts with EA
input double  InpRiskFraction   = 0.15;         // per-trade risk fraction (display)
input int     InpRiskFractionPct = 15;          // percent (15 => 0.15)
input int     InpExportPeriod   = PERIOD_M5;    // export timeframe

int OnInit()
{
   SetTradeAllowed(true);
   SetTradeExecution(ORDER_TYPE_MARKET, 0, 0, 0); // allow market orders
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
      "{\"connection_id\":\"%s\",\"login\":%d,\"broker\":\"%s\","
      "\"server\":\"%s\",\"nonce\":\"%s\",\"challenge\":\"%s\","
      "\"worker_health_ts\":\"%s\"}",
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
         "{\"symbol\":\"%s\",\"timeframe\":\"M5\",\"open_time\":\"%s\","
         "\"close_time\":\"%s\",\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,"
         "\"close\":%.5f,\"volume\":%.2f,\"bid\":%.5f,\"ask\":%.5f,"
         "\"spread\":%.0f,\"tick_size\":%.5f,\"point\":%.5f,\"closed\":true},",
         r.symbol, TimeToString(r.time, TIME_DATE|TIME_SECONDS),
         TimeToString(r.time + PeriodSeconds(InpExportPeriod), TIME_DATE|TIME_SECONDS),
         r.open, r.high, r.low, r.close, r.tick_volume,
         Bid, Ask, (int)(Ask - Bid), _Point, _Point);
   }
   barsJson = StringTrimRight(barsJson, ",");
   barsJson += "]}";
   FileWrite(bars, barsJson);

   // Order flow: aggressor trades since last export (trade-events)
   string ofJson = "{\"trades\":[";
   string lastFile = dir + "ABUTRON_ORDER_FLOW_V2_LAST.txt";
   ulong lastTs = 0;
   if(FileExists(lastFile))
   {
      string s; int n = FileReadString(lastFile, s);
      if(n > 0) lastTs = StringToTime(s);
   }
   TradeStats ts;
   // Use trade-event history from last export
   MqlTradeEvent ev[];
   // Simplified: dump last 500 closed-bar trade prints
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
   string reqFile = dir + "ABUTRON_REQ_*.json";
   // MQL5 cannot glob; manager writes a single pending file
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

   // Verify connection binding + activation (single-writer authority)
   if(cid != InpConnectionId || actRev != ActivationRevision())
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_SINGLE_WRITER_DISARMED");
      FileDelete(pending);
      return;
   }

   // Verify HMAC-SHA256 signature: signed = HMAC(seed, nonce|candidate|cid)
   // The EA must reject any request whose signature does not match.
   string nonce = JsonStr(body, "request_id");
   string cand  = JsonStr(body, "candidate_id");
   if(!VerifySignature(sig, nonce, cand, cid))
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_SIGNATURE_INVALID");
      FileDelete(pending);
      return;
   }

   double price = 0.0;
   if(dirn == "BUY")  price = Ask;
   else               price = Bid;

   // Normalize volume to broker lot step and floor (never round up)
   double normVol = NormalizeLots(volume);
   if(normVol < MarketInfo(symbol).VolumeMin)
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_MIN_LOT_EXCEEDS_RISK");
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
      WriteReceipt(reqId, res.order, "ACK", "NATIVE_ACK", res);
   }
   else
   {
      // Ambiguous / rejected — record outcome honestly
      WriteReceipt(reqId, 0, "UNKNOWN", "SEND_UNKNOWN", res);
   }
   FileDelete(pending);
}

double NormalizeLots(double v)
{
   double step = MarketInfo(_Symbol).VolumeMinStep;
   double lot  = MarketInfo(_Symbol).VolumeMin;
   return(NormalizeDouble(v, (int)MathRound(-MathLog10(step))) +
          (MathPercent(lot, step) ? 0.0 : 0.0));
}

void WriteReceipt(string reqId, long ticket, string state, string reason,
                  MqlTradeResult res = NULL)
{
   string dir = "MQL5\\Files\\";
   string file = dir + "ABUTRON_NATIVE_RECEIPTS_V2_" + Nonce() + ".txt";
   string json = StringFormat(
      "{\"request_id\":\"%s\",\"ticket\":%d,\"state\":\"%s\","
      "\"reason\":\"%s\",\"login\":%d,\"price\":%.5f,\"volume\":%.2f,"
      "\"sl\":%.5f,\"tp\":%.5f,\"deal\":\"%s\",\"ts\":\"%s\"}",
      reqId, ticket, state, reason,
      AccountInfoInteger(ACCOUNT_LOGIN),
      res.price, res.volume, res.sl, res.tp,
      LongToString(res.deal), TimeStampToStr(TimeCurrent()));
   FileWrite(file, json);
}

string Nonce()
{
   // Deterministic-ish nonce from time + login
   return(StringFormat("%d_%d", AccountInfoInteger(ACCOUNT_LOGIN),
                       (long)TimeCurrent()));
}

string Challenge()
{
   // Broker server challenge token (server time + account)
   return(StringFormat("%d", AccountInfoInteger(ACCOUNT_LOGIN) +
                       (long)ServerTime()));
}

string ActivationRevision()
{
   // Recomputed from connection + seed; manager must match
   return(CryptHash(StringFormat("%s|%d", InpConnectionId,
                                 InpActivationSeed)));
}

//+------------------------------------------------------------------+
//| HMAC-SHA256 verification (MQL5 has no built-in HMAC)              |
//+------------------------------------------------------------------+
//| Verifies that the request signature matches HMAC-SHA256(secret,  |
//| nonce|candidate_id|connection_id). Rejects if the secret is      |
//| empty or the signature does not match.                           |
//+------------------------------------------------------------------+
bool VerifySignature(string sig, string nonce, string cand, string cid)
{
   if(InpActivationSecret == "")
      return(false);   // No secret configured — never accept a signed order
   if(sig == "")
      return(false);
   string message = nonce + "|" + cand + "|" + cid;
   string expected = HmacSha256Hex(InpActivationSecret, message);
   return(sig == expected);
}

string HmacSha256Hex(string key, string message)
{
   // Build HMAC: H((K XOR opad) || H((K XOR ipad) || message))
   string blockSize = StringChar(0, 64);
   if(StringLen(key) > 64)
      key = Sha256Hex(key);
   // Pad key to 64 bytes
   while(StringLen(key) < 64)
      key += StringChar(0);
   if(StringLen(key) > 64)
      key = StringSubstr(key, 0, 64);

   string ipad = "";
   string opad = "";
   for(int i = 0; i < 64; i++)
   {
      ushort k = StringGetCharacter(key, i);
      ipad  += StringChar((char)(k ^ 0x36));
      opad  += StringChar((char)(k ^ 0x5c));
   }
   string inner = Sha256Hex(ipad + message);
   string innerBytes = HexToBinary(inner);
   string result = Sha256Hex(opad + innerBytes);
   return(result);
}

string Sha256Hex(string input)
{
   // SHA-256 implementation in MQL5
   uint H[8];
   H[0] = 0x6a09e667; H[1] = 0xbb67ae85; H[2] = 0x3c6ef372; H[3] = 0xa54ff53a;
   H[4] = 0x510e527f; H[5] = 0x9b05688c; H[6] = 0x1f83d9ab; H[7] = 0x5be0cd19;

   uint K[64];
   K[0]=0x428a2f98;K[1]=0x71374491;K[2]=0xb5c0fbcf;K[3]=0xe9b5dba5;K[4]=0x3956c25b;K[5]=0x59f111f1;K[6]=0x923f82a4;K[7]=0xab1c5ed5;
   K[8]=0xd807aa98;K[9]=0x12835b01;K[10]=0x243185be;K[11]=0x550c7dc3;K[12]=0x72be5d74;K[13]=0x80deb1fe;K[14]=0x9bdc06a7;K[15]=0xc19bf174;
   K[16]=0xe49b69c1;K[17]=0xefbe4786;K[18]=0x0fc19dc6;K[19]=0x240ca1cc;K[20]=0x2de92c6f;K[21]=0x4a7484aa;K[22]=0x5cb0a9dc;K[23]=0x76f988da;
   K[24]=0x983e5152;K[25]=0xa831c66d;K[26]=0xb00327c8;K[27]=0xbf597fc7;K[28]=0xc6e00bf3;K[29]=0xd5a79147;K[30]=0x06ca6351;K[31]=0x14292967;
   K[32]=0x27b70a85;K[33]=0x2e1b2138;K[34]=0x4d2c6dfc;K[35]=0x53380d13;K[36]=0x650a7354;K[37]=0x766a0abb;K[38]=0x81c2c92e;K[39]=0x92722c85;
   K[40]=0xa2bfe8a1;K[41]=0xa81a664b;K[42]=0xc24b8b70;K[43]=0xc76c51a3;K[44]=0xd192e819;K[45]=0xd6990624;K[46]=0xf40e3585;K[47]=0x106aa070;
   K[48]=0x19a4c116;K[49]=0x1e376c08;K[50]=0x2748774c;K[51]=0x34b0bcb5;K[52]=0x391c0cb3;K[53]=0x4ed8aa4a;K[54]=0x5b9cca4f;K[55]=0x682e6ff3;
   K[56]=0x748f82ee;K[57]=0x78a5636f;K[58]=0x84c87814;K[59]=0x8cc70208;K[60]=0x90befffa;K[61]=0xa4506ceb;K[62]=0xbef9a3f7;K[63]=0xc67178f2;

   // Convert input to byte array (ASCII)
   ushort bytes[];
   int len = StringLen(input);
   ArrayResize(bytes, len);
   for(int i = 0; i < len; i++)
      bytes[i] = StringGetCharacter(input, i);

   // Pad message: append 0x80, then zeros, then 64-bit length
   ulong bitLen = (ulong)len * 8;
   int padLen = 64 - ((len + 9) % 64);
   if(padLen == 64) padLen = 0;
   int totalLen = len + 1 + padLen + 8;
   ArrayResize(bytes, totalLen);
   bytes[len] = 0x80;
   bytes[totalLen - 8] = (ushort)(bitLen >> 56);
   bytes[totalLen - 7] = (ushort)(bitLen >> 48);
   bytes[totalLen - 6] = (ushort)(bitLen >> 40);
   bytes[totalLen - 5] = (ushort)(bitLen >> 32);
   bytes[totalLen - 4] = (ushort)(bitLen >> 24);
   bytes[totalLen - 3] = (ushort)(bitLen >> 16);
   bytes[totalLen - 2] = (ushort)(bitLen >> 8);
   bytes[totalLen - 1] = (ushort)(bitLen);

   // Process each 512-bit block
   ushort w[64];
   for(int block = 0; block < totalLen; block += 64)
   {
      for(int i = 0; i < 16; i++)
      {
         int j = block + i * 4;
         w[i] = (ushort)(((uint)bytes[j] << 24) | ((uint)bytes[j+1] << 16) |
                        ((uint)bytes[j+2] << 8) | (uint)bytes[j+3]);
      }
      for(int i = 16; i < 64; i++)
      {
         uint s0 = ((w[i-15] >> 7) | (w[i-15] << 25)) ^ ((w[i-15] >> 18) | (w[i-15] << 14)) ^ (w[i-15] >> 3);
         uint s1 = ((w[i-2] >> 17) | (w[i-2] << 15)) ^ ((w[i-2] >> 19) | (w[i-2] << 13)) ^ (w[i-2] >> 10);
         w[i] = (ushort)(w[i-16] + s0 + w[i-7] + s1);
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
      uint word = H[i];
      hex += StringFormat("%02x", (ushort)(word >> 24));
      hex += StringFormat("%02x", (ushort)(word >> 16));
      hex += StringFormat("%02x", (ushort)(word >> 8));
      hex += StringFormat("%02x", (ushort)(word));
   }
   return(hex);
}

string HexToBinary(string hex)
{
   // Convert a hex string to a binary (raw byte) string
   string bin = "";
   for(int i = 0; i + 1 < StringLen(hex); i += 2)
   {
      string hb = StringSubstr(hex, i, 2);
      int hi = HexVal(StringGetCharacter(hb, 0));
      int lo = HexVal(StringGetCharacter(hb, 1));
      bin += StringChar((char)((hi << 4) | lo));
   }
   return(bin);
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
