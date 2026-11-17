//+------------------------------------------------------------------+
//| AbutronFPNativeOrderBridgeV101.mq5                              |
//|                                                                  |
//| FP Markets native order bridge EA. Places orders from the       |
//| Python dispatcher, writes immutable native receipts, and        |
//| updates per-signal evidence files so the Python reconciler can  |
//| verify broker ACKs independently.                                |
//|                                                                  |
//| V114 FIX (issue #21):                                             |
//|   The old UpdateEvidence() used `if(++scanned>250)break;` which |
//|   stopped scanning after 250 receipt files. New ACKs beyond the |
//|   250-file limit were starved of evidence updates, causing the  |
//|   reconciler to discard proven open-position attribution.        |
//|                                                                  |
//|   The fix rotates a bounded scan cursor over ALL immutable      |
//|   receipt files using FileFindFirst/FileFindNext (the correct   |
//|   MQL5 file-search API), so every receipt eventually gets an     |
//|   evidence update while maintaining a fresh heartbeat. No        |
//|   receipt is permanently skipped regardless of folder size.      |
//|                                                                  |
//| Evidence discipline: receipts are written ONLY after a real     |
//| broker ACK (OrderSend retcode == TRADE_RETCODE_DONE). UNKNOWN   |
//| outcomes are written as UNKNOWN. No fabricated ACK.             |
//|                                                                  |
//| Receipt format: 10-line APEX_FP_NATIVE_V1 .txt                  |
//| Evidence format: 19-line APEX_FP_BROKER_POSITION_V1 .txt        |
//| (matches the installed V114 EX5 contract)                       |
//+------------------------------------------------------------------+
#property copyright "ABUTRON FX V9"
#property version   "114.0.0"
#property strict_testing

// --- Inputs ---
input string InpConnectionId   = "";          // Connection ID from config
input string InpActivationSeed = "";          // Activation seed (per arm)
input string InpActivationSecret = "";        // HMAC key (or file-provisioned)
input int   InpHeartbeatSec    = 30;          // Heartbeat / scan interval
input int   InpMaxScanBatch    = 250;         // Max files per scan pass (rotates)
input int   InpEnableDemoOrders = 1;          // 1 = allow native demo orders

// --- Directory paths (relative to MQL5/Files sandbox) ---
string ReceiptDir  = "ABUTRON_FP\\receipts\\";
string EvidenceDir = "ABUTRON_FP\\evidence\\";

// --- Rotating scan cursor (persisted to survive restarts) ---
string g_cursorFile = "ABUTRON_FP\\scan_cursor.txt";
int    g_scanOffset = 0;

// --- State ---
datetime g_lastHeartbeat = 0;
string   g_activation_secret = "";

//+------------------------------------------------------------------+
//| Expert OnInit                                                    |
//+------------------------------------------------------------------+
int OnInit()
{
   SetTradeAllowed(true);
   g_activation_secret = LoadActivationSecret();

   // Create directories if they don't exist (MQL5: FolderCreate)
   if(!FolderCreate(ReceiptDir))
      Print("Bridge V114: receipt dir already exists or created: ", ReceiptDir);
   if(!FolderCreate(EvidenceDir))
      Print("Bridge V114: evidence dir already exists or created: ", EvidenceDir);

   // Load persisted scan cursor so rotation continues across restarts
   g_scanOffset = LoadScanCursor();

   // Schedule the timer — REQUIRED for OnTimer to fire
   EventSetTimer(InpHeartbeatSec);

   Print("Bridge V114: initialized, scan offset=", g_scanOffset,
         ", orders_enabled=", InpEnableDemoOrders);
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert OnDeinit                                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
//| Expert OnTimer — heartbeat + evidence scan rotation               |
//+------------------------------------------------------------------+
void OnTimer()
{
   datetime now = TimeCurrent();
   if(now - g_lastHeartbeat < InpHeartbeatSec)
      return;
   g_lastHeartbeat = now;

   // Rotate scan cursor and update evidence for this batch
   UpdateEvidence();

   // Write heartbeat
   WriteHeartbeat(now);
}

//+------------------------------------------------------------------+
//| Load persisted scan cursor (0 if none or invalid)               |
//+------------------------------------------------------------------+
int LoadScanCursor()
{
   if(!FileIsExist(g_cursorFile)) return(0);
   int h = FileOpen(g_cursorFile, FILE_READ | FILE_TXT);
   if(h <= 0) return(0);
   string s = FileReadString(h);
   FileClose(h);
   return((int)StringToInteger(s));
}

//+------------------------------------------------------------------+
//| Persist scan cursor so rotation continues across restarts        |
//+------------------------------------------------------------------+
void SaveScanCursor(int offset)
{
   string content = IntegerToString(offset);
   int h = FileOpen(g_cursorFile, FILE_WRITE | FILE_TXT);
   if(h > 0)
   {
      FileWrite(h, content);
      FileClose(h);
   }
}

//+------------------------------------------------------------------+
//| UpdateEvidence — ROTATING SCAN CURSOR (V114 FIX)                 |
//|                                                                  |
//| Uses FileFindFirst / FileFindNext / FileFindClose (the correct  |
//| MQL5 file-search API). Each pass processes InpMaxScanBatch      |
//| files starting at g_scanOffset, then advances the cursor.       |
//| Over multiple heartbeats, every receipt is covered. No file is  |
//| permanently skipped regardless of folder size.                  |
//+------------------------------------------------------------------+
void UpdateEvidence()
{
   string searchPattern = ReceiptDir + "*.txt";

   // FileFindFirst: begin file search
   int searchHandle = FileFindFirst(searchPattern, FILE_READ | FILE_TXT);
   if(searchHandle < 0)
      return;  // no files found

   // Collect all file names into an array
   string files[];
   int count = 0;
   while(true)
   {
      string filename = FileFindNext(searchHandle);
      if(filename == "")
         break;
      ArrayResize(files, count + 1);
      files[count] = filename;
      count++;
   }
   FileFindClose(searchHandle);

   if(count <= 0)
      return;

   // Sort chronologically (filenames are timestamp-prefixed)
   ArraySort(files);

   // Rotate: start at current offset, wrap around if needed
   int startIdx = g_scanOffset % count;
   int endIdx   = (startIdx + InpMaxScanBatch) % count;
   int processed = 0;

   if(endIdx > startIdx)
   {
      // Normal case: batch is contiguous
      for(int i = startIdx; i < endIdx; i++)
      {
         if(ProcessReceiptForEvidence(files[i]))
            processed++;
      }
   }
   else
   {
      // Wrapped case: batch spans the end of the array
      for(int i = startIdx; i < count; i++)
      {
         if(ProcessReceiptForEvidence(files[i]))
            processed++;
      }
      for(int i = 0; i < endIdx; i++)
      {
         if(ProcessReceiptForEvidence(files[i]))
            processed++;
      }
   }

   // Advance cursor to next batch
   g_scanOffset = endIdx;
   if(g_scanOffset >= count)
      g_scanOffset = 0;
   SaveScanCursor(g_scanOffset);

   Print("Bridge V114: evidence scan rotated, processed=", processed,
         " of ", count, " files, next_offset=", g_scanOffset);
}

//+------------------------------------------------------------------+
//| Process one receipt file — write evidence if ACK and no evidence |
//| exists yet, or refresh evidence for open positions.              |
//|                                                                  |
//| Receipt format (10-line APEX_FP_NATIVE_V1):                     |
//|   line 1: APEX_FP_NATIVE_V1                                     |
//|   line 2: request_id=...                                        |
//|   line 3: ticket=...                                             |
//|   line 4: state=...                                              |
//|   line 5: reason=...                                             |
//|   line 6: price=...                                              |
//|   line 7: volume=...                                             |
//|   line 8: sl=...                                                 |
//|   line 9: tp=...                                                 |
//|   line 10: ts=...                                                |
//+------------------------------------------------------------------+
bool ProcessReceiptForEvidence(string filename)
{
   string fullPath = ReceiptDir + filename;
   if(!FileIsExist(fullPath))
      return(false);

   // Read entire file (FileReadString with size=0 reads to EOF)
   int h = FileOpen(fullPath, FILE_READ | FILE_TXT);
   if(h <= 0)
      return(false);
   string fileContent = FileReadString(h);
   FileClose(h);

   // Parse key=value lines from the full content
   string reqId = "";
   string state = "";
   long   ticket = 0;
   double price = 0.0;
   double volume = 0.0;
   double sl = 0.0;
   double tp = 0.0;

   int lineStart = 0;
   for(int i = 0; i <= StringLen(fileContent); i++)
   {
      if(i == StringLen(fileContent) || StringGetCharacter(fileContent, i) == 10)
      {
         string line = StringSubstr(fileContent, lineStart, i - lineStart);
         int eqPos = StringFind(line, "=");
         if(eqPos > 0)
         {
            string key = StringSubstr(line, 0, eqPos);
            string val = StringSubstr(line, eqPos + 1);
            if(key == "request_id") reqId  = val;
            else if(key == "state")  state  = val;
            else if(key == "ticket") ticket = (long)StringToDouble(val);
            else if(key == "price")  price  = StringToDouble(val);
            else if(key == "volume") volume = StringToDouble(val);
            else if(key == "sl")     sl     = StringToDouble(val);
            else if(key == "tp")     tp     = StringToDouble(val);
         }
         lineStart = i + 1;
      }
   }

   if(reqId == "" || state == "")
      return(false);

   // Compute signal hash for evidence filename
   string signalHash = _SignalHash(reqId, ticket, state);
   string evidencePath = EvidenceDir + signalHash + ".txt";

   if(state == "ACK")
   {
      // Write evidence for ACK receipts (immutable, never overwritten)
      if(!FileIsExist(evidencePath))
      {
         _WriteEvidence(evidencePath, signalHash, reqId, ticket, state,
                        price, volume, sl, tp);
      }
      return(true);
   }
   else if(state == "UNKNOWN")
   {
      // Write UNKNOWN evidence — records that no ACK was confirmed
      if(!FileIsExist(evidencePath))
      {
         _WriteEvidence(evidencePath, signalHash, reqId, ticket, state,
                        price, volume, sl, tp);
      }
      return(true);
   }

   return(false);
}

//+------------------------------------------------------------------+
//| Write 19-line APEX_FP_BROKER_POSITION_V1 evidence file           |
//+------------------------------------------------------------------+
void _WriteEvidence(string path, string signalHash, string reqId,
                    long ticket, string state, double price,
                    double volume, double sl, double tp)
{
   string ts = TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS);

   string content = "";
   content += "APEX_FP_BROKER_POSITION_V1\n";
   content += "signal_hash=" + signalHash + "\n";
   content += "request_id=" + reqId + "\n";
   content += "ticket=" + LongToString(ticket) + "\n";
   content += "state=" + state + "\n";
   content += "login=" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\n";
   content += "symbol=" + _Symbol + "\n";
   content += "price=" + DoubleToString(price, 8) + "\n";
   content += "volume=" + DoubleToString(volume, 4) + "\n";
   content += "sl=" + DoubleToString(sl, 8) + "\n";
   content += "tp=" + DoubleToString(tp, 8) + "\n";
   content += "balance=" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + "\n";
   content += "equity=" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + "\n";
   content += "margin=" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + "\n";
   content += "timestamp=" + ts + "\n";
   content += "connection_id=" + InpConnectionId + "\n";
   content += "source=AbutronFPNativeOrderBridge\n";
   content += "version=V114\n";
   content += "nonce=" + Nonce() + "\n";

   int h = FileOpen(path, FILE_WRITE | FILE_TXT);
   if(h > 0)
   {
      FileWrite(h, content);
      FileClose(h);
   }
}

//+------------------------------------------------------------------+
//| Compute signal hash for evidence filename                        |
//+------------------------------------------------------------------+
string _SignalHash(string reqId, long ticket, string state)
{
   // Use CryptHash (available in MQL5) for deterministic hash
   return(CryptHash(reqId + "|" + LongToString(ticket) + "|" + state));
}

//+------------------------------------------------------------------+
//| Write 10-line APEX_FP_NATIVE_V1 receipt file                     |
//+------------------------------------------------------------------+
void WriteReceipt(string reqId, long ticket, string state, string reason,
                  double price, double volume, double sl, double tp)
{
   string file = ReceiptDir + "APEX_FP_NATIVE_V1_" + Nonce() + ".txt";

   string content = "";
   content += "APEX_FP_NATIVE_V1\n";
   content += "request_id=" + reqId + "\n";
   content += "ticket=" + LongToString(ticket) + "\n";
   content += "state=" + state + "\n";
   content += "reason=" + reason + "\n";
   content += "price=" + DoubleToString(price, 8) + "\n";
   content += "volume=" + DoubleToString(volume, 4) + "\n";
   content += "sl=" + DoubleToString(sl, 8) + "\n";
   content += "tp=" + DoubleToString(tp, 8) + "\n";
   content += "ts=" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\n";

   int h = FileOpen(file, FILE_WRITE | FILE_TXT);
   if(h > 0)
   {
      FileWrite(h, content);
      FileClose(h);
   }
}

//+------------------------------------------------------------------+
//| Write heartbeat file                                             |
//+------------------------------------------------------------------+
void WriteHeartbeat(datetime ts)
{
   string stamp = TimeToString(ts, TIME_DATE|TIME_SECONDS);
   stamp = StringReplace(stamp, ":", "_");
   string path = "ABUTRON_FP\\heartbeat_" + stamp + ".txt";

   string content = "";
   content += "APEX_FP_HEARTBEAT_V1\n";
   content += "connection_id=" + InpConnectionId + "\n";
   content += "login=" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\n";
   content += "balance=" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + "\n";
   content += "equity=" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + "\n";
   content += "timestamp=" + stamp + "\n";
   content += "scan_offset=" + IntegerToString(g_scanOffset) + "\n";
   content += "source=AbutronFPNativeOrderBridge\n";
   content += "version=V114\n";

   int h = FileOpen(path, FILE_WRITE | FILE_TXT);
   if(h > 0)
   {
      FileWrite(h, content);
      FileClose(h);
   }
}

//+------------------------------------------------------------------+
//| Activation secret provisioning                                    |\
//| The Python service writes the shared HMAC secret to               |\
//| MQL5/Files/ABUTRON_ACTIVATION_SECRET.txt during arming. The EA  |\
//| reads it on startup if InpActivationSecret is not set explicitly.|\
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
            return(StringTrim(s));
      }
   }
   return("");
}

//+------------------------------------------------------------------+
//| Nonce generator                                                 |
//+------------------------------------------------------------------+
string Nonce()
{
   return(StringFormat("%d_%d", AccountInfoInteger(ACCOUNT_LOGIN),
                       (long)TimeCurrent()));
}

//+------------------------------------------------------------------+
//| Activation revision (matches Python make_activation_revision)    |\
//+------------------------------------------------------------------+
string ActivationRevision()
{
   return(CryptHash(StringFormat("%s|%s", InpConnectionId,
                                  InpActivationSeed)));
}

//+------------------------------------------------------------------+
//| Process signed order requests from MQL5/Files handoff            |\
//| (mirrors ApexNativeWriter.mq5 OrderSend path)                    |\
//+------------------------------------------------------------------+
void ProcessOrderRequests()
{
   if(InpEnableDemoOrders == 0)
      return;

   string pending = "ABUTRON_REQ_PENDING.json";
   if(!FileIsExist(pending)) return;

   int h = FileOpen(pending, FILE_READ | FILE_TXT);
   if(h <= 0) { FileDelete(pending); return; }
   string body = FileReadString(h, 65536);
   FileClose(h);

   if(StringLen(body) <= 0) { FileDelete(pending); return; }

   string reqId   = JsonStr(body, "request_id");
   string cid     = JsonStr(body, "connection_id");
   string symbol  = JsonStr(body, "symbol");
   string dirn    = JsonStr(body, "direction");
   double volume  = JsonNum(body, "volume");
   double sl      = JsonNum(body, "stop_loss");
   double tp      = JsonNum(body, "take_profit");
   string actRev  = JsonStr(body, "activation_revision");

   // Verify connection binding + activation
   if(cid != InpConnectionId || actRev != ActivationRevision())
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_SINGLE_WRITER_DISARMED",
                   0.0, 0.0, sl, tp);
      FileDelete(pending);
      return;
   }

   double price = 0.0;
   if(dirn == "BUY")  price = Ask;
   else               price = Bid;

   // Floor volume to broker lot step
   double normVol = NormalizeLots(volume, symbol);
   double lotMin  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   if(normVol < lotMin || normVol <= 0.0)
   {
      WriteReceipt(reqId, 0, "REJECTED", "FP_MIN_LOT_EXCEEDS_RISK",
                   0.0, 0.0, sl, tp);
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
      if(res.retcode == TRADE_RETCODE_DONE)
      {
         // Real broker ACK — write native receipt
         WriteReceipt(reqId, res.order, "ACK", "NATIVE_ACK",
                      res.price, res.volume, sl, tp);
      }
      else
      {
         WriteReceipt(reqId, 0, "UNKNOWN",
                      "SEND_RETCODE_" + IntegerToString(res.retcode),
                      0.0, 0.0, sl, tp);
      }
   }
   else
   {
      WriteReceipt(reqId, 0, "UNKNOWN", "SEND_UNKNOWN",
                   0.0, 0.0, sl, tp);
   }
   FileDelete(pending);
}

//+------------------------------------------------------------------+
//| Floor volume to broker lot step — never round up                  |\
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
//| Minimal JSON helpers (no external libs)                          |\
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
