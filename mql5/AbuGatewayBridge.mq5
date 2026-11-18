//+------------------------------------------------------------------+
//| AbuGatewayBridge.mq5                                             |
//| ABUTRON MT5 Gateway — hosted-account bridge EA                   |
//|                                                                  |
//| This EA is the ONLY order authority for an account connected to  |
//| the multi-tenant gateway. It:                                    |
//|   1. Pushes account / positions / orders / prices / candles /     |
//|      deals to the gateway over outbound HTTP (works through NAT)  |
//|   2. Pulls queued trade commands (flat key=value lines) and       |
//|      executes them with OrderSend()                              |
//|   3. Posts the real broker outcome back to the gateway            |
//|                                                                  |
//| It never fabricates an ACK: a rejected OrderSend is reported as   |
//| rejected with its retcode, and a transport failure leaves the     |
//| command unresolved so the API can surface an honest 202/UNKNOWN.  |
//|                                                                  |
//| IMPORTANT                                                         |
//|  * Attach either this EA OR ApexNativeWriter.mq5 — never both.    |
//|  * Allow the gateway URL in Tools → Options → Expert Advisors →   |
//|    "Allow WebRequest for listed URL".                             |
//|  * One EA per terminal. The terminal session is the broker auth.  |
//|  * This file is a template: compile it in MetaEditor and pin the  |
//|    resulting binary hash in your deployment.                      |
//+------------------------------------------------------------------+
#property copyright "ABUTRON FX V9"
#property version   "1.00"
#property description "Multi-tenant MT5 gateway bridge: state push + command pull"

//--- Gateway connection ------------------------------------------------
input string InpGatewayBaseUrl   = "http://127.0.0.1:19744"; // gateway base URL (no trailing /)
input string InpConnectionId     = "";                       // account id (acc_...)
input string InpBridgeToken      = "";                       // X-Bridge-Token
//--- Trading ----------------------------------------------------------
input string InpSymbols          = "EURUSD,GBPUSD,XAUUSD";   // symbols to publish
input int    InpStateIntervalSec = 5;                        // state push interval, seconds
input int    InpPollMs           = 250;                      // command poll throttle, ms
input int    InpCandleCount      = 60;                       // M5 bars per symbol
input int    InpDeviation        = 20;                       // slippage, points
input long   InpMagic            = 20261118;                 // magic number
input bool   InpEnableTrading    = true;                     // execute commands
input bool   InpVerbose          = true;                     // log each cycle

string   g_base       = "";
string   g_symbols[];
ulong    g_lastPushMs = 0;
ulong    g_lastPollMs = 0;
long     g_sequence   = 0;
bool     g_busy       = false;
bool     g_warnedUrl  = false;

//+------------------------------------------------------------------+
int OnInit()
{
   g_base = InpGatewayBaseUrl;
   while(StringLen(g_base) > 0 &&
         StringGetCharacter(g_base, StringLen(g_base) - 1) == '/')
      g_base = StringSubstr(g_base, 0, StringLen(g_base) - 1);

   if(InpConnectionId == "" || InpBridgeToken == "")
   {
      Print("[bridge] InpConnectionId and InpBridgeToken are required");
      return(INIT_PARAMETERS_INCORRECT);
   }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      Print("[bridge] warning: terminal trade is not allowed");

   int n = StringSplit(InpSymbols, ',', g_symbols);
   for(int i = 0; i < n; i++)
   {
      string trimmed = g_symbols[i];
      StringTrimLeft(trimmed);
      StringTrimRight(trimmed);
      g_symbols[i] = trimmed;
   }
   if(n <= 0)
   {
      ArrayResize(g_symbols, 1);
      g_symbols[0] = _Symbol;
   }

   EventSetTimer(1);
   Print("[bridge] started account=", InpConnectionId,
         " gateway=", g_base, " symbols=", InpSymbols);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   PushStateIfDue();
   PollCommandsIfDue();
}

void OnTick()
{
   // Ticks give lower command latency than the 1s timer. The poll is
   // throttled so a fast market cannot starve order processing.
   PollCommandsIfDue();
}

//+------------------------------------------------------------------+
//| HTTP helpers                                                     |
//+------------------------------------------------------------------+
bool HttpPost(string path, const string body, string &response, int &code)
{
   string url = g_base + path;
   string headers = "Content-Type: application/json\r\n"
                    "X-Bridge-Token: " + InpBridgeToken + "\r\n";
   char data[];
   int len = StringLen(body);
   if(len > 0)
   {
      // count excludes the terminating NUL StringToCharArray appends
      StringToCharArray(body, data, 0, len, CP_UTF8);
   }
   char result[];
   string resultHeaders = "";
   ResetLastError();
   code = WebRequest("POST", url, headers, 5000, data, result, resultHeaders);
   response = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(code == -1)
   {
      int err = GetLastError();
      if(err == 4060 && !g_warnedUrl)
      {
         Print("[bridge] WebRequest blocked. Add ", g_base,
               " to Tools → Options → Expert Advisors → "
               "Allow WebRequest for listed URL");
         g_warnedUrl = true;
      }
      return(false);
   }
   return(code >= 200 && code < 300);
}

bool HttpGet(string path, string &response, int &code)
{
   string url = g_base + path;
   string headers = "X-Bridge-Token: " + InpBridgeToken + "\r\n";
   char data[];
   char result[];
   string resultHeaders = "";
   ResetLastError();
   code = WebRequest("GET", url, headers, 7000, data, result, resultHeaders);
   response = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(code == -1)
   {
      int err = GetLastError();
      if(err == 4060 && !g_warnedUrl)
      {
         Print("[bridge] WebRequest blocked. Add ", g_base,
               " to Tools → Options → Expert Advisors → "
               "Allow WebRequest for listed URL");
         g_warnedUrl = true;
      }
      return(false);
   }
   return(code >= 200 && code < 300);
}

//+------------------------------------------------------------------+
//| State push                                                       |
//+------------------------------------------------------------------+
void PushStateIfDue()
{
   ulong now = GetTickCount64();
   int interval = InpStateIntervalSec;
   if(interval < 1) interval = 1;
   if(g_lastPushMs != 0 && now - g_lastPushMs < (ulong)(interval * 1000))
      return;
   g_lastPushMs = now;
   PushState();
}

void PushState()
{
   g_sequence++;
   string body = "{\"sequence\":" + IntegerToString(g_sequence);

   //--- account
   body += ",\"account\":{";
   body += "\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   body += ",\"broker\":\"" + J(AccountInfoString(ACCOUNT_COMPANY)) + "\"";
   body += ",\"server\":\"" + J(AccountInfoString(ACCOUNT_SERVER)) + "\"";
   body += ",\"name\":\"" + J(AccountInfoString(ACCOUNT_NAME)) + "\"";
   body += ",\"currency\":\"" + J(AccountInfoString(ACCOUNT_CURRENCY)) + "\"";
   body += ",\"leverage\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE));
   body += ",\"balance\":" + D(AccountInfoDouble(ACCOUNT_BALANCE));
   body += ",\"equity\":" + D(AccountInfoDouble(ACCOUNT_EQUITY));
   body += ",\"margin\":" + D(AccountInfoDouble(ACCOUNT_MARGIN));
   body += ",\"free_margin\":" + D(AccountInfoDouble(ACCOUNT_MARGIN_FREE));
   body += ",\"margin_level\":" + D(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL));
   body += ",\"trade_allowed\":" +
           (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "true" : "false");
   body += ",\"connected\":" +
           (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false");
   body += ",\"environment\":\"" +
           (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO
            ? "demo" : "live") + "\"";
   body += ",\"updated_at\":\"" + TimeIso(TimeCurrent()) + "\"}";

   //--- symbols & prices
   body += ",\"symbols\":[";
   for(int i = 0; i < ArraySize(g_symbols); i++)
   {
      string sym = g_symbols[i];
      if(!SymbolSelect(sym, true))
         continue;
      if(i > 0) body += ",";
      body += "{\"symbol\":\"" + J(sym) + "\"";
      body += ",\"description\":\"" + J(SymbolInfoString(sym, SYMBOL_DESCRIPTION)) + "\"";
      body += ",\"digits\":" + IntegerToString((int)SymbolInfoInteger(sym, SYMBOL_DIGITS));
      body += ",\"point\":" + D(SymbolInfoDouble(sym, SYMBOL_POINT));
      body += ",\"contract_size\":" + D(SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE));
      body += ",\"min_volume\":" + D(SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN));
      body += ",\"max_volume\":" + D(SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX));
      body += ",\"volume_step\":" + D(SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP));
      body += ",\"spread\":" + D((double)SymbolInfoInteger(sym, SYMBOL_SPREAD));
      body += ",\"trade_mode\":\"" +
              ((SymbolInfoInteger(sym, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_FULL)
               ? "full" : "restricted") + "\"}";
   }
   body += "],\"prices\":{";
   for(int i = 0; i < ArraySize(g_symbols); i++)
   {
      string sym = g_symbols[i];
      if(!SymbolSelect(sym, true))
         continue;
      if(i > 0) body += ",";
      body += "\"" + J(sym) + "\":{\"bid\":" +
              D(SymbolInfoDouble(sym, SYMBOL_BID)) + ",\"ask\":" +
              D(SymbolInfoDouble(sym, SYMBOL_ASK)) + ",\"time\":\"" +
              TimeIso(TimeCurrent()) + "\"}";
   }
   body += "}";

   //--- candles (M5)
   body += ",\"candles\":[";
   bool firstCandle = true;
   for(int i = 0; i < ArraySize(g_symbols); i++)
   {
      string sym = g_symbols[i];
      MqlRates rates[];
      int count = CopyRates(sym, PERIOD_M5, 0, InpCandleCount, rates);
      for(int k = 0; k < count; k++)
      {
         if(!firstCandle) body += ",";
         firstCandle = false;
         body += "{\"symbol\":\"" + J(sym) + "\",\"timeframe\":\"M5\"";
         body += ",\"open_time\":\"" + TimeIso(rates[k].time) + "\"";
         body += ",\"close_time\":\"" + TimeIso(rates[k].time + 300) + "\"";
         body += ",\"open\":" + D(rates[k].open);
         body += ",\"high\":" + D(rates[k].high);
         body += ",\"low\":" + D(rates[k].low);
         body += ",\"close\":" + D(rates[k].close);
         body += ",\"volume\":" + D((double)rates[k].tick_volume);
         body += ",\"closed\":" +
                 (rates[k].time + 300 <= TimeCurrent() ? "true" : "false") + "}";
      }
   }
   body += "]";

   //--- positions
   body += ",\"positions\":[";
   bool firstPos = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(!firstPos) body += ",";
      firstPos = false;
      string sym = PositionGetString(POSITION_SYMBOL);
      double current = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
                       ? SymbolInfoDouble(sym, SYMBOL_BID)
                       : SymbolInfoDouble(sym, SYMBOL_ASK);
      body += "{\"position_id\":\"" + U(ticket) + "\"";
      body += ",\"symbol\":\"" + J(sym) + "\"";
      body += ",\"side\":\"" +
              (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY
               ? "buy" : "sell") + "\"";
      body += ",\"volume\":" + D(PositionGetDouble(POSITION_VOLUME));
      body += ",\"open_price\":" + D(PositionGetDouble(POSITION_PRICE_OPEN));
      body += ",\"current_price\":" + D(current);
      body += ",\"stop_loss\":" + D(PositionGetDouble(POSITION_SL));
      body += ",\"take_profit\":" + D(PositionGetDouble(POSITION_TP));
      body += ",\"profit\":" + D(PositionGetDouble(POSITION_PROFIT));
      body += ",\"swap\":" + D(PositionGetDouble(POSITION_SWAP));
      body += ",\"commission\":0.0";
      body += ",\"comment\":\"" + J(PositionGetString(POSITION_COMMENT)) + "\"";
      body += ",\"magic\":" + IntegerToString((long)PositionGetInteger(POSITION_MAGIC));
      body += ",\"opened_at\":\"" + TimeIso((datetime)PositionGetInteger(POSITION_TIME)) + "\"}";
   }
   body += "]";

   //--- pending orders
   body += ",\"orders\":[";
   bool firstOrder = true;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || !OrderSelect(ticket))
         continue;
      if(!firstOrder) body += ",";
      firstOrder = false;
      long type = OrderGetInteger(ORDER_TYPE);
      string sym = OrderGetString(ORDER_SYMBOL);
      string side = "buy";
      string kind = "limit";
      if(type == ORDER_TYPE_SELL_LIMIT || type == ORDER_TYPE_SELL_STOP)
         side = "sell";
      if(type == ORDER_TYPE_BUY_STOP || type == ORDER_TYPE_SELL_STOP)
         kind = "stop";
      body += "{\"order_id\":\"" + U(ticket) + "\"";
      body += ",\"symbol\":\"" + J(sym) + "\"";
      body += ",\"side\":\"" + side + "\",\"type\":\"" + kind + "\"";
      body += ",\"volume\":" + D(OrderGetDouble(ORDER_VOLUME_CURRENT));
      body += ",\"price\":" + D(OrderGetDouble(ORDER_PRICE_OPEN));
      body += ",\"current_price\":" + D(
              side == "buy" ? SymbolInfoDouble(sym, SYMBOL_ASK)
                            : SymbolInfoDouble(sym, SYMBOL_BID));
      body += ",\"stop_loss\":" + D(OrderGetDouble(ORDER_SL));
      body += ",\"take_profit\":" + D(OrderGetDouble(ORDER_TP));
      body += ",\"state\":\"placed\"";
      body += ",\"comment\":\"" + J(OrderGetString(ORDER_COMMENT)) + "\"";
      body += ",\"created_at\":\"" + TimeIso((datetime)OrderGetInteger(ORDER_TIME_SETUP)) + "\"}";
   }
   body += "]";

   //--- deals and history orders (last 3 days, capped)
   body += ",\"deals\":[";
   bool firstDeal = true;
   datetime from = TimeCurrent() - 3 * 86400;
   if(HistorySelect(from, TimeCurrent() + 60))
   {
      int total = HistoryDealsTotal();
      int start = (total > 200) ? total - 200 : 0;
      for(int i = start; i < total; i++)
      {
         ulong ticket = HistoryDealGetTicket(i);
         if(ticket == 0) continue;
         if(!firstDeal) body += ",";
         firstDeal = false;
         body += "{\"deal_id\":\"" + U(ticket) + "\"";
         body += ",\"position_id\":\"" + U((ulong)HistoryDealGetInteger(ticket, DEAL_POSITION_ID)) + "\"";
         body += ",\"symbol\":\"" + J(HistoryDealGetString(ticket, DEAL_SYMBOL)) + "\"";
         body += ",\"side\":\"" +
                 (HistoryDealGetInteger(ticket, DEAL_TYPE) == DEAL_TYPE_BUY
                  ? "buy" : "sell") + "\"";
         body += ",\"volume\":" + D(HistoryDealGetDouble(ticket, DEAL_VOLUME));
         body += ",\"price\":" + D(HistoryDealGetDouble(ticket, DEAL_PRICE));
         body += ",\"profit\":" + D(HistoryDealGetDouble(ticket, DEAL_PROFIT));
         body += ",\"commission\":" + D(HistoryDealGetDouble(ticket, DEAL_COMMISSION));
         body += ",\"swap\":" + D(HistoryDealGetDouble(ticket, DEAL_SWAP));
         body += ",\"comment\":\"" + J(HistoryDealGetString(ticket, DEAL_COMMENT)) + "\"";
         body += ",\"time\":\"" + TimeIso((datetime)HistoryDealGetInteger(ticket, DEAL_TIME)) + "\"}";
      }
   }
   body += "]";

   body += ",\"history_orders\":[";
   bool firstHist = true;
   if(HistorySelect(from, TimeCurrent() + 60))
   {
      int total = HistoryOrdersTotal();
      int start = (total > 100) ? total - 100 : 0;
      for(int i = start; i < total; i++)
      {
         ulong ticket = HistoryOrderGetTicket(i);
         if(ticket == 0) continue;
         if(!firstHist) body += ",";
         firstHist = false;
         long type = HistoryOrderGetInteger(ticket, ORDER_TYPE);
         string side = (type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_BUY_STOP ||
                        type == ORDER_TYPE_BUY) ? "buy" : "sell";
         string kind = (type == ORDER_TYPE_BUY_STOP || type == ORDER_TYPE_SELL_STOP)
                       ? "stop" : "limit";
         body += "{\"order_id\":\"" + U(ticket) + "\"";
         body += ",\"symbol\":\"" + J(HistoryOrderGetString(ticket, ORDER_SYMBOL)) + "\"";
         body += ",\"side\":\"" + side + "\",\"type\":\"" + kind + "\"";
         body += ",\"volume\":" + D(HistoryOrderGetDouble(ticket, ORDER_VOLUME_INITIAL));
         body += ",\"price\":" + D(HistoryOrderGetDouble(ticket, ORDER_PRICE_OPEN));
         body += ",\"stop_loss\":" + D(HistoryOrderGetDouble(ticket, ORDER_SL));
         body += ",\"take_profit\":" + D(HistoryOrderGetDouble(ticket, ORDER_TP));
         body += ",\"state\":\"" +
                 (HistoryOrderGetInteger(ticket, ORDER_STATE) == ORDER_STATE_CANCELED
                  ? "cancelled" : "filled") + "\"";
         body += ",\"comment\":\"" + J(HistoryOrderGetString(ticket, ORDER_COMMENT)) + "\"";
         body += ",\"created_at\":\"" + TimeIso((datetime)HistoryOrderGetInteger(ticket, ORDER_TIME_SETUP)) + "\"}";
      }
   }
   body += "]}";

   string response;
   int code = 0;
   if(!HttpPost("/internal/bridge/" + InpConnectionId + "/state", body, response, code))
   {
      if(InpVerbose)
         Print("[bridge] state push failed code=", code, " len=", StringLen(body));
      return;
   }
   if(InpVerbose)
      Print("[bridge] state pushed seq=", g_sequence, " bytes=", StringLen(body));
}

//+------------------------------------------------------------------+
//| Command pull + execute                                           |
//+------------------------------------------------------------------+
void PollCommandsIfDue()
{
   if(!InpEnableTrading || g_busy)
      return;
   ulong now = GetTickCount64();
   int throttle = MathMax(100, InpPollMs);
   if(g_lastPollMs != 0 && now - g_lastPollMs < (ulong)throttle)
      return;
   g_lastPollMs = now;

   string response;
   int code = 0;
   if(!HttpGet("/internal/bridge/" + InpConnectionId +
               "/commands?wait=0&format=text", response, code))
      return;
   if(StringLen(response) == 0)
      return;

   g_busy = true;
   bool executed = false;
   string lines[];
   int n = StringSplit(response, StringGetCharacter("\n", 0), lines);
   for(int i = 0; i < n; i++)
   {
      string line = lines[i];
      StringTrimLeft(line);
      StringTrimRight(line);
      if(StringLen(line) == 0)
         continue;
      ProcessCommandLine(line);
      executed = true;
   }
   g_busy = false;
   // Publish the new state immediately after a fill so API readers and
   // WebSocket subscribers see the position without an extra interval.
   if(executed)
   {
      PushState();
      g_lastPushMs = GetTickCount64();
   }
}

void ProcessCommandLine(const string line)
{
   string parts[];
   int n = StringSplit(line, StringGetCharacter(";", 0), parts);
   string commandId = "", kind = "", symbol = "", side = "", orderType = "";
   string positionId = "", orderId = "", comment = "";
   double volume = 0, sl = 0, tp = 0, price = 0;

   for(int i = 0; i < n; i++)
   {
      int eq = StringFind(parts[i], "=");
      if(eq <= 0) continue;
      string key = StringSubstr(parts[i], 0, eq);
      string value = StringSubstr(parts[i], eq + 1);
      if(key == "command_id")      commandId = value;
      else if(key == "kind")       kind = value;
      else if(key == "symbol")     symbol = value;
      else if(key == "side")       side = value;
      else if(key == "volume")     volume = StringToDouble(value);
      else if(key == "stop_loss")  sl = StringToDouble(value);
      else if(key == "take_profit") tp = StringToDouble(value);
      else if(key == "price")      price = StringToDouble(value);
      else if(key == "order_type") orderType = value;
      else if(key == "position_id") positionId = value;
      else if(key == "order_id")   orderId = value;
      else if(key == "comment")    comment = value;
   }
   if(commandId == "" || kind == "")
      return;

   bool accepted = false;
   string retcode = "UNKNOWN", message = "";
   ulong orderTicket = 0, dealTicket = 0, positionTicket = 0;
   double fillPrice = 0, filledVolume = 0;

   if(kind == "market")
   {
      accepted = SendMarket(symbol, side, volume, sl, tp, comment,
                            orderTicket, dealTicket, positionTicket,
                            fillPrice, filledVolume, retcode, message);
   }
   else if(kind == "pending")
   {
      accepted = SendPending(symbol, side, volume, price, sl, tp, orderType,
                             comment, orderTicket, fillPrice, retcode,
                             message);
   }
   else if(kind == "modify")
   {
      accepted = ModifyPosition((ulong)StringToInteger(positionId), sl, tp,
                                fillPrice, retcode, message);
   }
   else if(kind == "close")
   {
      accepted = ClosePosition((ulong)StringToInteger(positionId), volume,
                               dealTicket, fillPrice, filledVolume,
                               retcode, message);
   }
   else if(kind == "cancel")
   {
      accepted = CancelOrder((ulong)StringToInteger(orderId), retcode,
                             message);
   }
   else
   {
      retcode = "UNKNOWN_COMMAND";
      message = "unsupported kind: " + kind;
   }

   if(InpVerbose)
      Print("[bridge] ", kind, " ", commandId, " accepted=", accepted,
            " retcode=", retcode, " msg=", message);

   if(positionTicket == 0)
      positionTicket = (ulong)StringToInteger(positionId);
   PostResult(commandId, accepted, retcode, message, orderTicket,
              positionTicket, dealTicket, fillPrice,
              filledVolume > 0 ? filledVolume : volume);
}

//+------------------------------------------------------------------+
//| Execution                                                        |
//+------------------------------------------------------------------+
bool SendMarket(const string symbol, const string side, const double volume,
                const double sl, const double tp, const string comment,
                ulong &orderTicket, ulong &dealTicket, ulong &positionTicket,
                double &fillPrice, double &filledVolume, string &retcode,
                string &message)
{
   if(!SymbolSelect(symbol, true))
   {
      retcode = "SYMBOL_NOT_FOUND";
      message = symbol;
      return(false);
   }
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   bool buy = (side == "buy");
   req.action = TRADE_ACTION_DEAL;
   req.symbol = symbol;
   req.volume = NormalizeVolume(symbol, volume);
   req.type = buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price = buy ? SymbolInfoDouble(symbol, SYMBOL_ASK)
                   : SymbolInfoDouble(symbol, SYMBOL_BID);
   req.sl = Np(symbol, sl);
   req.tp = Np(symbol, tp);
   req.deviation = InpDeviation;
   req.magic = (ulong)InpMagic;
   req.comment = StringSubstr(comment, 0, 31);
   req.type_time = ORDER_TIME_GTC;
   req.type_filling = (ENUM_ORDER_TYPE_FILLING)FillingMode(symbol);

   bool ok = OrderSend(req, res);
   orderTicket = res.order;
   dealTicket = res.deal;
   positionTicket = PositionFromDeal(res.deal);
   fillPrice = res.price;
   filledVolume = res.volume;
   retcode = RetcodeToString(res.retcode);
   message = res.comment;
   return(ok && (res.retcode == TRADE_RETCODE_DONE ||
                 res.retcode == TRADE_RETCODE_PLACED ||
                 res.retcode == TRADE_RETCODE_DONE_PARTIAL));
}

bool SendPending(const string symbol, const string side, const double volume,
                 const double price, const double sl, const double tp,
                 const string orderType, const string comment,
                 ulong &orderTicket, double &fillPrice, string &retcode,
                 string &message)
{
   if(!SymbolSelect(symbol, true))
   {
      retcode = "SYMBOL_NOT_FOUND";
      message = symbol;
      return(false);
   }
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);

   bool buy = (side == "buy");
   bool stop = (orderType == "stop");
   req.action = TRADE_ACTION_PENDING;
   req.symbol = symbol;
   req.volume = NormalizeVolume(symbol, volume);
   req.price = Np(symbol, price);
   req.sl = Np(symbol, sl);
   req.tp = Np(symbol, tp);
   req.deviation = InpDeviation;
   req.magic = (ulong)InpMagic;
   req.comment = StringSubstr(comment, 0, 31);
   req.type_time = ORDER_TIME_GTC;
   req.type_filling = (ENUM_ORDER_TYPE_FILLING)FillingMode(symbol, true);
   if(buy)
      req.type = stop ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_BUY_LIMIT;
   else
      req.type = stop ? ORDER_TYPE_SELL_STOP : ORDER_TYPE_SELL_LIMIT;

   bool ok = OrderSend(req, res);
   orderTicket = res.order;
   fillPrice = req.price;
   retcode = RetcodeToString(res.retcode);
   message = res.comment;
   return(ok && (res.retcode == TRADE_RETCODE_DONE ||
                 res.retcode == TRADE_RETCODE_PLACED));
}

bool ModifyPosition(const ulong ticket, const double sl, const double tp,
                    double &currentPrice, string &retcode, string &message)
{
   if(ticket == 0 || !PositionSelectByTicket(ticket))
   {
      retcode = "POSITION_NOT_FOUND";
      message = "no such position";
      return(false);
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_SLTP;
   req.position = ticket;
   req.symbol = symbol;
   req.sl = Np(symbol, sl);
   req.tp = Np(symbol, tp);
   currentPrice = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY
                  ? SymbolInfoDouble(symbol, SYMBOL_BID)
                  : SymbolInfoDouble(symbol, SYMBOL_ASK);
   bool ok = OrderSend(req, res);
   retcode = RetcodeToString(res.retcode);
   message = res.comment;
   return(ok && (res.retcode == TRADE_RETCODE_DONE ||
                 res.retcode == TRADE_RETCODE_PLACED));
}

bool ClosePosition(const ulong ticket, const double volume, ulong &dealTicket,
                   double &closePrice, double &closedVolume, string &retcode,
                   string &message)
{
   if(ticket == 0 || !PositionSelectByTicket(ticket))
   {
      retcode = "POSITION_NOT_FOUND";
      message = "no such position";
      return(false);
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   bool isBuy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   double positionVolume = PositionGetDouble(POSITION_VOLUME);
   double closeVolume = volume > 0 ? MathMin(volume, positionVolume)
                                   : positionVolume;

   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_DEAL;
   req.position = ticket;
   req.symbol = symbol;
   req.volume = NormalizeVolume(symbol, closeVolume);
   req.type = isBuy ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   req.price = isBuy ? SymbolInfoDouble(symbol, SYMBOL_BID)
                     : SymbolInfoDouble(symbol, SYMBOL_ASK);
   req.deviation = InpDeviation;
   req.magic = (ulong)InpMagic;
   req.comment = "api-close";
   req.type_time = ORDER_TIME_GTC;
   req.type_filling = (ENUM_ORDER_TYPE_FILLING)FillingMode(symbol);

   bool ok = OrderSend(req, res);
   dealTicket = res.deal;
   closePrice = res.price;
   closedVolume = res.volume > 0 ? res.volume : req.volume;
   retcode = RetcodeToString(res.retcode);
   message = res.comment;
   return(ok && (res.retcode == TRADE_RETCODE_DONE ||
                 res.retcode == TRADE_RETCODE_DONE_PARTIAL));
}

bool CancelOrder(const ulong ticket, string &retcode, string &message)
{
   if(ticket == 0 || !OrderSelect(ticket))
   {
      retcode = "ORDER_NOT_FOUND";
      message = "no such order";
      return(false);
   }
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_REMOVE;
   req.order = ticket;
   bool ok = OrderSend(req, res);
   retcode = RetcodeToString(res.retcode);
   message = res.comment;
   return(ok && (res.retcode == TRADE_RETCODE_DONE ||
                 res.retcode == TRADE_RETCODE_PLACED));
}

//+------------------------------------------------------------------+
//| Result upload                                                    |
//+------------------------------------------------------------------+
void PostResult(const string commandId, const bool accepted,
                const string retcode, const string message,
                const ulong orderTicket, const ulong positionTicket,
                const ulong dealTicket, const double price,
                const double volume)
{
   string body = "{\"accepted\":" + (accepted ? "true" : "false");
   body += ",\"retcode\":\"" + J(retcode) + "\"";
   body += ",\"message\":\"" + J(message) + "\"";
   body += ",\"order_id\":\"" + U(orderTicket) + "\"";
   body += ",\"position_id\":\"" + U(positionTicket) + "\"";
   body += ",\"deal_id\":\"" + U(dealTicket) + "\"";
   body += ",\"price\":" + D(price);
   body += ",\"volume\":" + D(volume) + "}";

   string response;
   int code = 0;
   if(!HttpPost("/internal/bridge/" + InpConnectionId +
                "/commands/" + commandId + "/result", body, response, code))
      Print("[bridge] result upload failed command=", commandId,
            " code=", code, " body=", response);
}

//+------------------------------------------------------------------+
//| Small helpers                                                    |
//+------------------------------------------------------------------+
ulong PositionFromDeal(const ulong dealTicket)
{
   if(dealTicket == 0) return(0);
   // The deal may not be in the cached history window yet, so refresh it.
   HistorySelect(TimeCurrent() - 86400, TimeCurrent() + 60);
   if(!HistoryDealSelect(dealTicket)) return(0);
   return((ulong)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID));
}

string U(const ulong value)
{
   return(IntegerToString((long)value));
}

string D(const double value)
{
   return(DoubleToString(value, 5));
}

double Np(const string symbol, const double price)
{
   if(price == 0) return(0.0);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   return(NormalizeDouble(price, digits));
}

double NormalizeVolume(const string symbol, const double volume)
{
   double minv = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxv = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0) step = 0.01;
   double v = MathRound(volume / step) * step;
   if(v < minv) v = minv;
   if(v > maxv) v = maxv;
   int digits = 0;
   double s = step;
   while(digits < 8 && MathAbs(s - MathRound(s)) > 0.000001)
   {
      s *= 10;
      digits++;
   }
   return(NormalizeDouble(v, MathMax(0, digits)));
}

int FillingMode(const string symbol, const bool pending = false)
{
   int mode = (int)SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   // Pending orders require RETURN on many brokers regardless of the
   // symbol's market filling flags.
   if(pending) return(ORDER_FILLING_RETURN);
   if((mode & SYMBOL_FILLING_FOK) != 0) return(ORDER_FILLING_FOK);
   if((mode & SYMBOL_FILLING_IOC) != 0) return(ORDER_FILLING_IOC);
   return(ORDER_FILLING_RETURN);
}

string RetcodeToString(const uint code)
{
   switch(code)
   {
      case TRADE_RETCODE_REQUOTE:            return("REQUOTE");
      case TRADE_RETCODE_REJECT:             return("REJECT");
      case TRADE_RETCODE_CANCEL:             return("CANCEL");
      case TRADE_RETCODE_PLACED:             return("PLACED");
      case TRADE_RETCODE_DONE:               return("DONE");
      case TRADE_RETCODE_DONE_PARTIAL:       return("DONE_PARTIAL");
      case TRADE_RETCODE_ERROR:              return("ERROR");
      case TRADE_RETCODE_TIMEOUT:            return("TIMEOUT");
      case TRADE_RETCODE_INVALID:            return("INVALID");
      case TRADE_RETCODE_INVALID_VOLUME:     return("INVALID_VOLUME");
      case TRADE_RETCODE_INVALID_PRICE:      return("INVALID_PRICE");
      case TRADE_RETCODE_INVALID_STOPS:      return("INVALID_STOPS");
      case TRADE_RETCODE_TRADE_DISABLED:     return("TRADE_DISABLED");
      case TRADE_RETCODE_MARKET_CLOSED:      return("MARKET_CLOSED");
      case TRADE_RETCODE_NO_MONEY:           return("NO_MONEY");
      case TRADE_RETCODE_PRICE_CHANGED:      return("PRICE_CHANGED");
      case TRADE_RETCODE_PRICE_OFF:          return("PRICE_OFF");
      case TRADE_RETCODE_TOO_MANY_REQUESTS:  return("TOO_MANY_REQUESTS");
      case TRADE_RETCODE_NO_CHANGES:         return("NO_CHANGES");
      case TRADE_RETCODE_CONNECTION:         return("CONNECTION");
      case TRADE_RETCODE_LIMIT_VOLUME:       return("LIMIT_VOLUME");
      case TRADE_RETCODE_LIMIT_ORDERS:       return("LIMIT_ORDERS");
      case TRADE_RETCODE_LIMIT_POSITIONS:    return("LIMIT_POSITIONS");
      case TRADE_RETCODE_POSITION_CLOSED:    return("POSITION_CLOSED");
      case TRADE_RETCODE_INVALID_FILL:       return("INVALID_FILL");
      case TRADE_RETCODE_LONG_ONLY:          return("LONG_ONLY");
      case TRADE_RETCODE_SHORT_ONLY:         return("SHORT_ONLY");
      case TRADE_RETCODE_CLOSE_ONLY:         return("CLOSE_ONLY");
      case TRADE_RETCODE_FROZEN:             return("FROZEN");
      case TRADE_RETCODE_LOCKED:             return("LOCKED");
      default:                               return("RETCODE_" + IntegerToString((int)code));
   }
}

string TimeIso(const datetime value)
{
   // MT5 chart/account timestamps are in broker server time. Convert to
   // UTC so the gateway's ISO timestamps mean the same thing everywhere.
   long offset = (long)TimeCurrent() - (long)TimeGMT();
   datetime gmt = (datetime)((long)value - offset);
   return(TimeToString(gmt, TIME_DATE | TIME_SECONDS) + "Z");
}

string J(const string value)
{
   string out = "";
   int len = StringLen(value);
   for(int i = 0; i < len; i++)
   {
      ushort c = StringGetCharacter(value, i);
      if(c == '"')      out += "\\\"";
      else if(c == '\\') out += "\\\\";
      else if(c == '\n') out += "\\n";
      else if(c == '\r') out += "\\r";
      else if(c == '\t') out += "\\t";
      else              out += ShortToString(c);
   }
   return(out);
}
//+------------------------------------------------------------------+
