//+------------------------------------------------------------------+
//| ABUTRON V9 DEMO Market Data Collector V3 - READ ONLY             |
//| CopyTicks broker tape, quote ticks and MarketBookGet depth.      |
//| It has NO order functions and refuses LIVE terminals.           |
//+------------------------------------------------------------------+
#property copyright "ABUTRON FX V9"
#property version "3.01"
#property strict
#property indicator_chart_window
#property indicator_plots 0
#property description "DEMO-only broker market tick and DOM export, no orders"
input int ExportIntervalSec=5;
input int MaxTicksPerPoll=1500;
input int MaxFileSizeMB=24;
input bool CollectBook=true;
input string InpSymbolList="";

string g_symbol="";
string g_server="";
long g_login=0;
ulong g_cursor_msc=0;
int g_cursor_count=0;
bool g_book_on=false;
datetime g_book_last=0;
datetime g_heartbeat_last=0;
int g_print_count=0;
int g_quote_count=0;
int g_depth_count=0;
int g_drop_count=0;
string g_symbols[];           // multi-symbol collection list
int    g_symbol_books[];      // parallel: 1 if DOM subscribed for that symbol
ulong  g_symbol_cursors[];    // parallel: per-symbol cursor ms
int    g_symbol_skip[];       // parallel: per-symbol skip count at cursor

string Sanitize(string value)
{
   string out="";
   for(int k=0;k<StringLen(value);k++)
   {
      ushort ch=StringGetCharacter(value,k);
      if((ch>=48 && ch<=57)||(ch>=65 && ch<=90)||
         (ch>=97 && ch<=122)||ch==46||ch==45||ch==95||ch==32)
         out+=ShortToString(ch);
   }
   return out;
}
string SanitizeFile(string value)
{
   // Filename-safe variant: replaces spaces with underscores so that
   // Deriv symbols like "Volatility 50 Index.0" produce a clean
   // filename while preserving the broker identity in JSON content.
   string out="";
   for(int k=0;k<StringLen(value);k++)
   {
      ushort ch=StringGetCharacter(value,k);
      if((ch>=48 && ch<=57)||(ch>=65 && ch<=90)||
         (ch>=97 && ch<=122)||ch==46||ch==45||ch==95)
         out+=ShortToString(ch);
      else if(ch==32)
         out+="_";
   }
   return out;
}
string JsonEscape(string value)
{
   StringReplace(value,"\\","\\\\");
   StringReplace(value,"\"","\\\"");
   StringReplace(value,"\r","");
   StringReplace(value,"\n","");
   return value;
}
string RootFile(const string sym)
{
   MqlDateTime t;
   TimeToStruct(TimeGMT(),t);
   return "ABUTRON_V9_NATIVE\\ABUTRON_NATIVE_MARKET_V3_"+
      SanitizeFile(sym)+"_"+StringFormat("%04d%02d%02d",t.year,t.mon,t.day)+".jsonl";
}
string Context(const string sym)
{
   return "\"schema\":\"abutron.v9.mt5.market_microstructure.readonly.v3\","+
          "\"symbol\":\""+JsonEscape(sym)+"\","+
          "\"broker_server\":\""+JsonEscape(g_server)+"\","+
          "\"account_login\":\""+IntegerToString(g_login)+"\","+
          "\"environment\":\"DEMO\",";
}
int OpenAppend(const string sym)
{
   string path=RootFile(sym);
   int h=FileOpen(path,FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ,0,CP_UTF8);
   if(h==INVALID_HANDLE)
   {
      Print("ABUTRON V3 exporter cannot open ",path," code=",GetLastError());
      return INVALID_HANDLE;
   }
   ulong cap=(ulong)MaxFileSizeMB*1024*1024;
   if(FileSize(h)>=cap)
   {
      FileClose(h);
      g_drop_count++;
      return INVALID_HANDLE;
   }
   FileSeek(h,0,SEEK_END);
   return h;
}
bool Append(int h,string json)
{
   if(h==INVALID_HANDLE) return false;
   if(FileSize(h)>=((ulong)MaxFileSizeMB*1024*1024)) {g_drop_count++;return false;}
   return FileWriteString(h,json+"\n")>0;
}
string UtcTimestamp()
{
   return IntegerToString((long)TimeGMT());
}
bool IsValidDemo()
{
   return AccountInfoInteger(ACCOUNT_TRADE_MODE)==ACCOUNT_TRADE_MODE_DEMO &&
          AccountInfoInteger(ACCOUNT_LOGIN)==g_login &&
          AccountInfoString(ACCOUNT_SERVER)==g_server &&
          TerminalInfoInteger(TERMINAL_CONNECTED)!=0;
}
int OnInit()
{
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO)
   {
      Print("ABUTRON V3 exporter refuses non-DEMO account");
      return INIT_FAILED;
   }
   g_login=(long)AccountInfoInteger(ACCOUNT_LOGIN);
   g_server=AccountInfoString(ACCOUNT_SERVER);
   g_symbol=_Symbol;
   // Build the symbol collection list. If InpSymbolList is empty,
   // default to the chart's symbol (single-symbol mode).
   if(InpSymbolList!="")
   {
      // Use pipe (|) as delimiter to avoid conflicts with the comma
      // separator used by iCustom() name=value parameter pairs.
      string parts[];
      int n=StringSplit(InpSymbolList,StringGetCharacter("|",0),parts);
      for(int i=0;i<n;i++)
      {
         string s=parts[i];
         // Trim whitespace
         StringTrimLeft(s);
         StringTrimRight(s);
         if(s=="")continue;
         int next=ArraySize(g_symbols);
         if(ArrayResize(g_symbols,next+1)!=next+1)
            return INIT_FAILED;
         g_symbols[next]=s;
      }
   }
   if(ArraySize(g_symbols)==0)
   {
      ArrayResize(g_symbols,1);
      g_symbols[0]=_Symbol;
   }
   if(g_login<=0||g_server==""||_Symbol==""||
      ExportIntervalSec<1||ExportIntervalSec>60||
      MaxTicksPerPoll<100||MaxTicksPerPoll>4000||
      MaxFileSizeMB<1||MaxFileSizeMB>100)
      return INIT_PARAMETERS_INCORRECT;
   // Validate all symbols in the list
   for(int i=0;i<ArraySize(g_symbols);i++)
   {
      if(Sanitize(g_symbols[i])!=g_symbols[i])
         return INIT_PARAMETERS_INCORRECT;
   }
   // Initialize per-symbol state arrays
   ArrayResize(g_symbol_books,ArraySize(g_symbols));
   ArrayResize(g_symbol_cursors,ArraySize(g_symbols));
   ArrayResize(g_symbol_skip,ArraySize(g_symbols));
   ulong base_cursor=(ulong)MathMax(1,(long)TimeCurrent()-30)*1000;
   for(int i=0;i<ArraySize(g_symbols);i++)
   {
      g_symbol_cursors[i]=base_cursor;
      g_symbol_skip[i]=0;
      g_symbol_books[i]=0;
      if(CollectBook)
      {
         if(MarketBookAdd(g_symbols[i]))
            g_symbol_books[i]=1;
      }
   }
   g_book_on=ArraySize(g_symbol_books)>0;
   if(!EventSetTimer(ExportIntervalSec))
   {
      for(int i=0;i<ArraySize(g_symbols);i++)
         if(g_symbol_books[i]!=0)MarketBookRelease(g_symbols[i]);
      return INIT_FAILED;
   }
   Print("ABUTRON V3 readonly DEMO collector initialized login=",
         g_login," symbols=",IntegerToString((long)ArraySize(g_symbols)),
         " DOM_subscribed=",g_book_on);
   for(int i=0;i<ArraySize(g_symbols);i++)
      Print("ABUTRON_V3_SYMBOL:",g_symbols[i]);
   return INIT_SUCCEEDED;
}
void ExportTicks()
{
   for(int si=0;si<ArraySize(g_symbols);si++)
   {
      string sym=g_symbols[si];
      MqlTick ticks[];
      int n=CopyTicks(sym,ticks,COPY_TICKS_ALL,g_symbol_cursors[si],(uint)MaxTicksPerPoll);
      if(n<=0)continue;
      int h=OpenAppend(sym);
      if(h==INVALID_HANDLE)continue;
      int skip_at_cursor=g_symbol_skip[si];
      for(int i=0;i<n;i++)
      {
         MqlTick t=ticks[i];
         ulong ms=t.time_msc;
         if(ms<g_symbol_cursors[si])continue;
         if(ms==g_symbol_cursors[si] && skip_at_cursor>0)
         {
            skip_at_cursor--;
            continue;
         }
         if(ms!=g_symbol_cursors[si])
         {
            g_symbol_cursors[si]=ms;
            g_symbol_skip[si]=0;
         }
         g_symbol_skip[si]++;
         bool buy=((t.flags&TICK_FLAG_BUY)==TICK_FLAG_BUY);
         bool sell=((t.flags&TICK_FLAG_SELL)==TICK_FLAG_SELL);
         bool traded=((t.flags&TICK_FLAG_LAST)==TICK_FLAG_LAST)&&
                     (buy!=sell)&&t.last>0&&(t.volume_real>0||t.volume>0);
         bool quote=((t.flags&TICK_FLAG_BID)==TICK_FLAG_BID)||
                    ((t.flags&TICK_FLAG_ASK)==TICK_FLAG_ASK);
         // Do not infer aggressor side from bid/ask quote changes.
         if(!traded && !quote)continue;
         string kind=(traded?"MARKET_PRINT":"QUOTE");
         string side=(traded?(buy?"BUY":"SELL"):"NONE");
         int digits=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
         string record="{"+Context(sym)+
            "\"record_type\":\""+kind+"\","+
            "\"time_msc\":"+IntegerToString((long)t.time_msc)+","+
            "\"flags\":"+IntegerToString((long)t.flags)+","+
            "\"price\":"+DoubleToString(t.last,digits)+","+
            "\"bid\":"+DoubleToString(t.bid,digits)+","+
            "\"ask\":"+DoubleToString(t.ask,digits)+","+
            "\"volume_real\":"+DoubleToString(t.volume_real,8)+","+
            "\"volume\":"+IntegerToString((long)t.volume)+","+
            "\"aggressor_side\":\""+side+"\"}";
         if(!Append(h,record))break;
         if(traded)g_print_count++;
         else g_quote_count++;
      }
      FileFlush(h);
      FileClose(h);
   }
}
void ExportDepth()
{
   if(!IsValidDemo())return;
   datetime now=TimeGMT();
   for(int si=0;si<ArraySize(g_symbols);si++)
   {
      if(g_symbol_books[si]==0)continue;
      string sym=g_symbols[si];
      MqlBookInfo book[];
      if(!MarketBookGet(sym,book)||ArraySize(book)==0)continue;
      int h=OpenAppend(sym);
      if(h==INVALID_HANDLE)continue;
      int digits=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
      string body="{"+Context(sym)+
         "\"record_type\":\"DOM\","+
         "\"time_msc\":"+IntegerToString((long)now*1000)+","+
         "\"levels\":[";
      for(int i=0;i<ArraySize(book)&&i<80;i++)
      {
         if(i>0)body+=",";
         body+="{\"book_type\":"+IntegerToString((int)book[i].type)+
               ",\"price\":"+DoubleToString(book[i].price,digits)+
               ",\"volume_real\":"+DoubleToString(book[i].volume_real,8)+
               ",\"volume\":"+IntegerToString((long)book[i].volume)+"}";
      }
      body+="]}";
      if(Append(h,body))g_depth_count++;
      FileFlush(h);FileClose(h);
   }
}
void ExportHeartbeat()
{
   for(int si=0;si<ArraySize(g_symbols);si++)
   {
      string sym=g_symbols[si];
      int h=OpenAppend(sym);
      if(h==INVALID_HANDLE)continue;
      string body="{"+Context(sym)+
         "\"record_type\":\"STATUS\","+
         "\"time_msc\":"+IntegerToString((long)TimeGMT()*1000)+","+
         "\"print_records\":"+IntegerToString(g_print_count)+","+
         "\"quote_records\":"+IntegerToString(g_quote_count)+","+
         "\"dom_records\":"+IntegerToString(g_depth_count)+","+
         "\"dropped_records\":"+IntegerToString(g_drop_count)+","+
         "\"dom_subscribed\":"+(g_symbol_books[si]!=0?"true":"false")+"}";
      Append(h,body);FileFlush(h);FileClose(h);
   }
}
void OnTimer()
{
   if(!IsValidDemo())return;
   ExportTicks();
   datetime now=TimeGMT();
   if(now-g_book_last>=5){ExportDepth();g_book_last=now;}
   if(now-g_heartbeat_last>=60){ExportHeartbeat();g_heartbeat_last=now;}
}
void OnBookEvent(const string &symbol)
{
   // Check if the event symbol is in our collection list
   int idx=-1;
   for(int si=0;si<ArraySize(g_symbols);si++)
   {
      if(g_symbols[si]==symbol)
      {
         idx=si;
         break;
      }
   }
   if(idx<0||g_symbol_books[idx]==0)return;
   datetime now=TimeGMT();
   if(now-g_book_last<5)return;
   // Export depth for just this symbol
   MqlBookInfo book[];
   if(!MarketBookGet(symbol,book)||ArraySize(book)==0)return;
   int h=OpenAppend(symbol);
   if(h==INVALID_HANDLE)return;
   int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
   string body="{"+Context(symbol)+
      "\"record_type\":\"DOM\","+
      "\"time_msc\":"+IntegerToString((long)now*1000)+","+
      "\"levels\":[";
   for(int i=0;i<ArraySize(book)&&i<80;i++)
   {
      if(i>0)body+=",";
      body+="{\"book_type\":"+IntegerToString((int)book[i].type)+
            ",\"price\":"+DoubleToString(book[i].price,digits)+
            ",\"volume_real\":"+DoubleToString(book[i].volume_real,8)+
            ",\"volume\":"+IntegerToString((long)book[i].volume)+"}";
   }
   body+="]}";
   if(Append(h,body))g_depth_count++;
   FileFlush(h);FileClose(h);
   g_book_last=now;
}
void OnDeinit(const int reason)
{
   EventKillTimer();
   for(int si=0;si<ArraySize(g_symbols);si++)
      if(g_symbol_books[si]!=0)MarketBookRelease(g_symbols[si]);
}


// Indicator variant runs alongside the chart's existing EA.
// No replacement of an order-execution expert or broker order API calls.
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[], const double &close[],
                const long &tick_volume[], const long &volume[],
                const int &spread[])
{
   return rates_total;
}
