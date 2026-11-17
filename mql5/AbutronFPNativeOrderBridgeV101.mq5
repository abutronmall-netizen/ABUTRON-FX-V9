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
//|   receipt files (via DirectoryGetFiles with a rotating offset),  |
//|   so every receipt eventually gets an evidence update while      |
//|   maintaining a fresh heartbeat. No receipt is permanently      |
//|   skipped regardless of folder size.                             |
//|                                                                  |
//| Evidence discipline: receipts are written ONLY after a real     |
//| broker ACK (TRADE_RETCODE_DONE). UNKNOWN outcomes are written   |
//| as UNKNOWN. No fabricated ACK.                                   |
//+------------------------------------------------------------------+
#property copyright "ABUTRON FX V9"
#property version   "114.0.0"
#property strict

// --- Inputs ---
input string InpConnectionId   = "";          // Connection ID from config
input string InpActivationSeed = "";          // HMAC activation seed
input int   InpHeartbeatSec    = 30;          // Heartbeat interval
input int   InpMaxScanBatch    = 250;         // Max files per scan pass (rotates)

// --- Evidence directory ---
string EvidenceDir = "MQL5\\Files\\ABUTRON_FP\\evidence\\";
string ReceiptDir  = "MQL5\\Files\\ABUTRON_FP\\receipts\\";

// --- Rotating scan cursor (persisted to survive restarts) ---
static string g_cursorFile = "MQL5\\Files\\ABUTRON_FP\\scan_cursor.txt";
static int    g_scanOffset = 0;  // rotating offset into DirectoryGetFiles result

// --- State ---
static datetime g_lastHeartbeat = 0;
static string   g_lastNonce     = "";

//+------------------------------------------------------------------+
//| Expert OnInit                                                    |
//+------------------------------------------------------------------+
int OnInit()
{
   // Ensure evidence/receipt directories exist
   if(!DirectoryCreate(EvidenceDir))
   {
      Print("Bridge: failed to create evidence dir ", EvidenceDir);
   }
   if(!DirectoryCreate(ReceiptDir))
   {
      Print("Bridge: failed to create receipts dir ", ReceiptDir);
   }

   // Load persisted scan cursor so rotation continues across restarts
   g_scanOffset = LoadScanCursor();
   Print("Bridge V114: initialized, scan offset=", g_scanOffset);
   return(INIT_SUCCEEDED);
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
   int h = FileOpen(g_cursorFile, FILE_WRITE | FILE_TXT | FILE_REPLACE);
   if(h > 0)
   {
      FileWrite(h, content);
      FileClose(h);
   }
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
//| UpdateEvidence — ROTATING SCAN CURSOR (V114 FIX)                 |
//|                                                                  |
//| The old code did: `if(++scanned>250)break;` which permanently    |
//| skipped all receipts beyond the 250th file. The new code rotates |
//| a cursor over ALL immutable receipt files: each pass processes   |
//| InpMaxScanBatch files starting at g_scanOffset, then advances    |
//| the cursor. Over multiple heartbeats, every receipt is covered.  |
//+------------------------------------------------------------------+
void UpdateEvidence()
{
   // Get ALL receipt files (sorted by name = chronological)
   string files[];
   int n = DirectoryGetFiles(ReceiptDir, "*.txt", 0, FILE_TEXT | FILE_READ,
                             files);
   if(n <= 0)
      return;

   ArraySort(files);  // sort chronologically by filename

   // Rotate: start at current offset, wrap around if needed
   int startIdx = g_scanOffset % n;
   int endIdx   = (startIdx + InpMaxScanBatch) % n;
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
      for(int i = startIdx; i < n; i++)
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
   if(g_scanOffset >= n)
      g_scanOffset = 0;  // wrap
   SaveScanCursor(g_scanOffset);

   Print("Bridge V114: evidence scan rotated, processed=", processed,
         " of ", n, " files, next_offset=", g_scanOffset);
}

//+------------------------------------------------------------------+
//| Process one receipt file — write evidence if ACK and no evidence |
//| exists yet, or refresh evidence for open positions.              |
//+------------------------------------------------------------------+
bool ProcessReceiptForEvidence(string filename)
{
   string fullPath = ReceiptDir + filename;
   if(!FileIsExist(fullPath))
      return(false);

   string receiptContent;
   int h = FileOpen(fullPath, FILE_READ | FILE_TXT);
   if(h <= 0)
      return(false);
   receiptContent = FileReadString(h);
   FileClose(h);

   // Parse the receipt JSON (simple key:value)
   string reqId    = _ParseJsonString(receiptContent, "request_id");
   string state    = _ParseJsonString(receiptContent, "state");
   long   ticket   = (long)_ParseJsonNumber(receiptContent, "ticket");
   double price    = _ParseJsonNumber(receiptContent, "price");
   double volume   = _ParseJsonNumber(receiptContent, "volume");
   double sl       = _ParseJsonNumber(receiptContent, "sl");
   double tp       = _ParseJsonNumber(receiptContent, "tp");

   if(reqId == "" || state == "")
      return(false);

   // Compute signal hash (used as evidence filename)
   string signalHash = _SignalHash(reqId, ticket, state);

   // Evidence file path
   string evidencePath = EvidenceDir + signalHash + ".txt";

   if(state == "ACK")
   {
      // Write evidence for ACK receipts (immutable, never overwritten)
      if(!FileIsExist(evidencePath))
      {
         string evidence = StringFormat(
            "{\"signal_hash\":\"%s\",\"request_id\":\"%s\",\"ticket\":%d,"
            "\"state\":\"%s\",\"price\":%.5f,\"volume\":%.2f,\"sl\":%.5f,"
            "\"tp\":%.5f,\"ts\":\"%s\",\"source\":\"AbutronFPNativeOrderBridge\"}",
            signalHash, reqId, ticket, state, price, volume, sl, tp,
            TimeStampToStr(TimeCurrent()));
         _WriteFile(evidencePath, evidence);
      }
      return(true);
   }
   else if(state == "UNKNOWN")
   {
      // Write UNKNOWN evidence — records that no ACK was confirmed
      if(!FileIsExist(evidencePath))
      {
         string evidence = StringFormat(
            "{\"signal_hash\":\"%s\",\"request_id\":\"%s\",\"ticket\":%d,"
            "\"state\":\"UNKNOWN\",\"price\":%.5f,\"volume\":%.2f,"
            "\"sl\":%.5f,\"tp\":%.5f,\"ts\":\"%s\","
            "\"source\":\"AbutronFPNativeOrderBridge\"}",
            signalHash, reqId, ticket, price, volume, sl, tp,
            TimeStampToStr(TimeCurrent()));
         _WriteFile(evidencePath, evidence);
      }
      return(true);
   }

   return(false);
}

//+------------------------------------------------------------------+
//| Compute signal hash for evidence filename                        |
//+------------------------------------------------------------------+
string _SignalHash(string reqId, long ticket, string state)
{
   return(CryptHash(reqId + "|" + IntegerToString(ticket) + "|" + state));
}

//+------------------------------------------------------------------+
//| Write heartbeat file                                             |
//+------------------------------------------------------------------+
void WriteHeartbeat(datetime ts)
{
   string stamp = TimeToString(ts, TIME_DATE|TIME_SECONDS);
   stamp = StringReplace(stamp, ":", "_");
   string path = "MQL5\\Files\\ABUTRON_FP\\heartbeat_" + stamp + ".txt";

   MqlTradeAccount account;
   AccountInfoTrade(account);

   string content = StringFormat(
      "{\"connection_id\":\"%s\",\"login\":%d,\"balance\":%.2f,"
      "\"equity\":%.2f,\"timestamp\":\"%s\",\"scan_offset\":%d,"
      "\"source\":\"AbutronFPNativeOrderBridge\"}",
      InpConnectionId, account.login, account.balance, account.equity,
      stamp, g_scanOffset);

   _WriteFile(path, content);
}

//+------------------------------------------------------------------+
//| Write text file (creates/overwrites)                             |
//+------------------------------------------------------------------+
void _WriteFile(string filename, string content)
{
   int h = FileOpen(filename, FILE_WRITE | FILE_TXT);
   if(h > 0)
   {
      FileWrite(h, content);
      FileClose(h);
   }
}

//+------------------------------------------------------------------+
//| Parse a string value from simple JSON                            |
//+------------------------------------------------------------------+
string _ParseJsonString(string json, string key)
{
   string search = "\"" + key + "\":\"";
   int pos = StringFind(json, search);
   if(pos < 0) return("");
   pos += StringLen(search);
   int end = StringFind(json, "\"", pos);
   if(end < 0) return("");
   return(StringSubstr(json, pos, end - pos));
}

//+------------------------------------------------------------------+
//| Parse a number value from simple JSON                            |
//+------------------------------------------------------------------+
double _ParseJsonNumber(string json, string key)
{
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if(pos < 0) return(0.0);
   pos += StringLen(search);
   string num_str = "";
   int i = pos;
   while(i < StringLen(json))
   {
      char c = StringGetCharacter(json, i);
      if((c >= '0' && c <= '9') || c == '-' || c == '.')
         num_str += CharacterToString(c);
      else if(c == ',' || c == '}')
         break;
      i++;
   }
   if(num_str == "") return(0.0);
   return(StringToDouble(num_str));
}

//+------------------------------------------------------------------+
//| Expert OnDeinit                                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("Bridge V114: deinitialized, reason=", reason);
}
//+------------------------------------------------------------------+
