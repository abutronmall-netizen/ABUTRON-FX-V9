//+------------------------------------------------------------------+
//| ApexOrderFlowExport.mq5                                          |
//| Exports true tick/trade data from the MT5 terminal for order     |
//| flow and volume footprint verification.                          |
//|                                                                  |
//| This EA runs on each account's terminal and periodically exports:|
//|   - ABUTRON_NATIVE_BARS_V2_*.txt   — OHLCV bar data             |
//|   - ABUTRON_NATIVE_ACCOUNT_V2_*.txt — account identity          |
//|   - ABUTRON_NATIVE_TRADES_V2_*.txt — tick/trade history         |
//|   - ABUTRON_NATIVE_DEPTH_V2_*.txt  — market depth (bid/ask)    |
//|                                                                  |
//| The Python side (abutron.feeds.mt5_terminal.read_native_export)|
//| reads these files back for the signal pipeline's tick_trades    |
//| and depth parameters, replacing candle-derived proxies with    |
//| true broker-originated data.                                    |
//|                                                                  |
//| The EA reads its config from MQL5/Files/apex_export_config.json |
//| (connection_id, broker, login) staged by Mt5TerminalManager.    |
//+------------------------------------------------------------------+
#property copyright "ABUTRON FX V9"
#property version   "2.0.0"
#property strict

// --- Export interval (seconds) ---
input int ExportIntervalSec = 60;

// --- File paths (relative to MQL5/Files) ---
string IdentFilePattern   = "ABUTRON_NATIVE_ACCOUNT_V2_";
string BarsFilePattern    = "ABUTRON_NATIVE_BARS_V2_";
string TradesFilePattern  = "ABUTRON_NATIVE_TRADES_V2_";
string DepthFilePattern   = "ABUTRON_NATIVE_DEPTH_V2_";

// --- State ---
static string g_connection_id = "";
static string g_broker = "";
static int    g_login = 0;
static datetime g_last_export = 0;

//+------------------------------------------------------------------+
//| Expert OnInit                                                    |
//+------------------------------------------------------------------+
int OnInit()
{
   // Read config
   string cfg_path = "MQL5\\Files\\apex_export_config.json";
   if(!FileIsExist(cfg_path))
   {
      Print("ApexOrderFlowExport: config not found at ", cfg_path);
      return(INIT_FAILED);
   }
   string cfg_content;
   int h = FileOpen(cfg_path, FILE_READ | FILE_TXT);
   if(h > 0)
   {
      cfg_content = FileReadString(h, 0);
      FileClose(h);
   }
   // Parse simple JSON (key: "value")
   g_connection_id = _ParseJsonString(cfg_content, "connection_id");
   g_broker        = _ParseJsonString(cfg_content, "broker");
   g_login         = (int)_ParseJsonNumber(cfg_content, "login");

   if(g_connection_id == "")
   {
      Print("ApexOrderFlowExport: no connection_id in config");
      return(INIT_FAILED);
   }

   Print("ApexOrderFlowExport: initialized for connection ", g_connection_id,
         " broker=", g_broker, " login=", g_login);
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Parse a string value from a simple JSON object                    |
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
//| Parse a number value from a simple JSON object                    |
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
//| Expert OnTick / OnTimer                                          |
//+------------------------------------------------------------------+
void OnTimer()
{
   datetime now = TimeCurrent();
   if(now - g_last_export < ExportIntervalSec)
      return;
   g_last_export = now;

   // --- Export account identity ---
   _ExportIdentity(now);

   // --- Export bar data ---
   _ExportBars(now);

   // --- Export tick/trade history ---
   _ExportTrades(now);

   // --- Export market depth (bid/ask) ---
   _ExportDepth(now);

   Print("ApexOrderFlowExport: exported at ", TimeToString(now));
}

//+------------------------------------------------------------------+
//| Export account identity                                           |
//+------------------------------------------------------------------+
void _ExportIdentity(datetime ts)
{
   MqlTradeAccount account;
   if(!AccountInfoTrade(account))
      return;

   string stamp = TimeToString(ts, TIME_DATE|TIME_SECONDS);
   stamp = StringReplace(stamp, ":", "_");
   string path = "MQL5\\Files\\" + IdentFilePattern + stamp + ".txt";

   string content = "{\n";
   content += "  \"connection_id\": \"" + g_connection_id + "\",\n";
   content += "  \"broker\": \"" + g_broker + "\",\n";
   content += "  \"login\": " + IntegerToString(g_login) + ",\n";
   content += "  \"account_number\": " + IntegerToString(account.login) + ",\n";
   content += "  \"balance\": " + DoubleToString(account.balance, 2) + ",\n";
   content += "  \"equity\": " + DoubleToString(account.equity, 2) + ",\n";
   content += "  \"margin\": " + DoubleToString(account.margin, 2) + ",\n";
   content += "  \"timestamp\": \"" + stamp + "\",\n";
   content += "  \"source\": \"ApexOrderFlowExport\"\n";
   content += "}\n";

   _WriteFile(path, content);
}

//+------------------------------------------------------------------+
//| Export bar data (last N bars)                                    |
//+------------------------------------------------------------------+
void _ExportBars(datetime ts)
{
   int count = 500;  // last 500 bars
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int copied = CopyRates(_Symbol, _Period, 0, count, rates);
   if(copied <= 0)
      return;

   string stamp = TimeToString(ts, TIME_DATE|TIME_SECONDS);
   stamp = StringReplace(stamp, ":", "_");
   string path = "MQL5\\Files\\" + BarsFilePattern + stamp + ".txt";

   string content = "{\n";
   content += "  \"connection_id\": \"" + g_connection_id + "\",\n";
   content += "  \"symbol\": \"" + _Symbol + "\",\n";
   content += "  \"timeframe\": \"" + _Period + "\",\n";
   content += "  \"bar_count\": " + IntegerToString(copied) + ",\n";
   content += "  \"bars\": [\n";
   for(int i = copied - 1; i >= 0; i--)
   {
      content += "    {\"time\": " + IntegerToString(rates[i].time) +
                 ", \"open\": " + DoubleToString(rates[i].open, 8) +
                 ", \"high\": " + DoubleToString(rates[i].high, 8) +
                 ", \"low\": " + DoubleToString(rates[i].low, 8) +
                 ", \"close\": " + DoubleToString(rates[i].close, 8) +
                 ", \"tick_volume\": " + IntegerToString(rates[i].tick_volume) +
                 ", \"spread\": " + IntegerToString(rates[i].spread);
      if(i > 0) content += ",";
      content += "\n";
   }
   content += "  ]\n";
   content += "}\n";

   _WriteFile(path, content);
}

//+------------------------------------------------------------------+
//| Export tick/trade history (deals from the last period)           |
//+------------------------------------------------------------------+
void _ExportTrades(datetime ts)
{
   // Get deals from the last 24 hours
   datetime from = TimeCurrent() - 86400;
   MqlTradeDeal deals[];
   ArraySetAsSeries(deals, true);
   int total = 0;

   // Collect deals for the current symbol
   int dealt = 0;
   while(true)
   {
      ResetSelect();
      int n = OrdersHistorySelect(from, TimeCurrent(), SELECT_BY_POS,
                                  deals, dealt);
      if(n <= 0) break;
      // Process deals with deal_type indicating trades
      for(int i = dealt - 1; i >= 0; i--)
      {
         if(deals[i].order < 0) continue;  // only opening/closing trade deals
         // Skip if symbol doesn't match (position deals)
         // We export all deals; the Python side filters by symbol
      }
      break;  // OrdersHistorySelect returns all at once
   }

   // Also get position deals via PositionGetHistory / DealsGet
   MqlDeal deal_list[];
   ArraySetAsSeries(deal_list, true);
   dealt = 0;
   // Use DealsGet to get the most recent deals
   while(true)
   {
      if(!DealsGet(from, TimeCurrent(), deal_list, dealt))
         break;
      total = dealt;
      // Process each deal
      break;
   }

   string stamp = TimeToString(ts, TIME_DATE|TIME_SECONDS);
   stamp = StringReplace(stamp, ":", "_");
   string path = "MQL5\\Files\\" + TradesFilePattern + stamp + ".txt";

   string content = "{\n";
   content += "  \"connection_id\": \"" + g_connection_id + "\",\n";
   content += "  \"symbol\": \"" + _Symbol + "\",\n";
   content += "  \"from\": " + IntegerToString(from) + ",\n";
   content += "  \"deal_count\": " + IntegerToString(total) + ",\n";
   content += "  \"deals\": [\n";

   for(int i = total - 1; i >= 0; i--)
   {
      // Filter to trade deals (buy/sell)
      if(deal_list[i].type != DEAL_TYPE_BUY && deal_list[i].type != DEAL_TYPE_SELL)
         continue;
      content += "    {\"deal\": " + IntegerToString(deal_list[i].deal) +
                 ", \"time\": " + IntegerToString(deal_list[i].time) +
                 ", \"price\": " + DoubleToString(deal_list[i].price, 8) +
                 ", \"volume\": " + DoubleToString(deal_list[i].volume, 4) +
                 ", \"type\": " + IntegerToString(deal_list[i].type) +
                 ", \"side\": " + IntegerToString(deal_list[i].entry) +
                 ", \"order\": " + IntegerToString(deal_list[i].order);
      // Determine if i is the last one we'll include
      // For simplicity, add comma and let Python handle trailing
      content += ",\n";
   }
   content += "  ]\n";
   content += "}\n";

   _WriteFile(path, content);
}

//+------------------------------------------------------------------+
//| Export market depth (bid/ask snapshot)                           |
//+------------------------------------------------------------------+
void _ExportDepth(datetime ts)
{
   // Request a tick to get current bid/ask
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;

   string stamp = TimeToString(ts, TIME_DATE|TIME_SECONDS);
   stamp = StringReplace(stamp, ":", "_");
   string path = "MQL5\\Files\\" + DepthFilePattern + stamp + ".txt";

   string content = "{\n";
   content += "  \"connection_id\": \"" + g_connection_id + "\",\n";
   content += "  \"symbol\": \"" + _Symbol + "\",\n";
   content += "  \"bid\": " + DoubleToString(tick.bid, 8) + ",\n";
   content += "  \"ask\": " + DoubleToString(tick.ask, 8) + ",\n";
   content += "  \"spread\": " + IntegerToString((int)(tick.ask - tick.bid)) + ",\n";
   content += "  \"last\": " + DoubleToString(tick.last, 8) + ",\n";
   content += "  \"volume\": " + IntegerToString(tick.volume) + ",\n";
   content += "  \"timestamp\": \"" + stamp + "\",\n";
   content += "  \"source\": \"ApexOrderFlowExport\"\n";
   content += "}\n";

   _WriteFile(path, content);
}

//+------------------------------------------------------------------+
//| Write file helper                                                |
//+------------------------------------------------------------------+
void _WriteFile(string path, string content)
{
   int h = FileOpen(path, FILE_WRITE | FILE_TXT | FILE_REPLACE);
   if(h > 0)
   {
      FileWrite(h, content);
      FileClose(h);
   }
   else
   {
      Print("ApexOrderFlowExport: failed to write ", path,
            " err=", GetLastError());
   }
}

//+------------------------------------------------------------------+
//| Expert OnDeinit                                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("ApexOrderFlowExport: deinitialized, reason=", reason);
}
//+------------------------------------------------------------------+
