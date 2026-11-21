//+------------------------------------------------------------------+
//| ABUTRON V9 DEMO Market Data Collector V3 - READ ONLY             |
//| CopyTicks broker tape, quote ticks and MarketBookGet depth.      |
//| It has NO order functions and refuses LIVE terminals.           |
//+------------------------------------------------------------------+
#property copyright "ABUTRON FX V9"
#property version "3.00"
#property strict
#property indicator_chart_window
#property indicator_plots 0
#property description "DEMO-only broker market tick and DOM export, no orders"
input int ExportIntervalSec=5;
input int MaxTicksPerPoll=1500;
input int MaxFileSizeMB=24;
input bool CollectBook=true;

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
string RootFile()
{
   MqlDateTime t;
   TimeToStruct(TimeGMT(),t);
   return "ABUTRON_V9_NATIVE\\ABUTRON_NATIVE_MARKET_V3_"+
      SanitizeFile(g_symbol)+"_"+StringFormat("%04d%02d%02d",t.year,t.mon,t.day)+".jsonl";
}
string Context()
{
   return "\"schema\":\"abutron.v9.mt5.market_microstructure.readonly.v3\","+
          "\"symbol\":\""+JsonEscape(g_symbol)+"\","+
          "\"broker_server\":\""+JsonEscape(g_server)+"\","+
          "\"account_login\":\""+IntegerToString(g_login)+"\","+
          "\"environment\":\"DEMO\",";
}
int OpenAppend()
{
   string path=RootFile();
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
   if(g_login<=0||g_server==""||g_symbol==""||
      Sanitize(g_symbol)!=g_symbol||ExportIntervalSec<1||ExportIntervalSec>60||
      MaxTicksPerPoll<100||MaxTicksPerPoll>4000||
      MaxFileSizeMB<1||MaxFileSizeMB>100)
      return INIT_PARAMETERS_INCORRECT;
   // Start with 30 seconds history, then maintain a timestamp + same-ms cursor.
   g_cursor_msc=(ulong)MathMax(1,(long)TimeCurrent()-30)*1000;
   g_cursor_count=0;
   g_book_on=CollectBook && MarketBookAdd(g_symbol);
   if(!EventSetTimer(ExportIntervalSec))
   {
      if(g_book_on)MarketBookRelease(g_symbol);
      return INIT_FAILED;
   }
   Print("ABUTRON V3 readonly DEMO collector initialized login=",
         g_login," symbol=",g_symbol," DOM_subscribed=",g_book_on);
   return INIT_SUCCEEDED;
}
void ExportTicks()
{
   MqlTick ticks[];
   int n=CopyTicks(g_symbol,ticks,COPY_TICKS_ALL,g_cursor_msc,(uint)MaxTicksPerPoll);
   if(n<=0)return;
   int h=OpenAppend();
   if(h==INVALID_HANDLE)return;
   int skip_at_cursor=g_cursor_count;
   for(int i=0;i<n;i++)
   {
      MqlTick t=ticks[i];
      ulong ms=t.time_msc;
      if(ms<g_cursor_msc)continue;
      if(ms==g_cursor_msc && skip_at_cursor>0)
      {
         skip_at_cursor--;
         continue;
      }
      if(ms!=g_cursor_msc)
      {
         g_cursor_msc=ms;
         g_cursor_count=0;
      }
      g_cursor_count++;
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
      string record="{"+Context()+
        "\"record_type\":\""+kind+"\","+
        "\"time_msc\":"+IntegerToString((long)t.time_msc)+","+
        "\"flags\":"+IntegerToString((long)t.flags)+","+
        "\"price\":"+DoubleToString(t.last,_Digits)+","+
        "\"bid\":"+DoubleToString(t.bid,_Digits)+","+
        "\"ask\":"+DoubleToString(t.ask,_Digits)+","+
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
void ExportDepth()
{
   if(!g_book_on||!IsValidDemo())return;
   MqlBookInfo book[];
   if(!MarketBookGet(g_symbol,book)||ArraySize(book)==0)return;
   int h=OpenAppend();
   if(h==INVALID_HANDLE)return;
   string body="{"+Context()+
      "\"record_type\":\"DOM\","+
      "\"time_msc\":"+IntegerToString((long)TimeGMT()*1000)+","+
      "\"levels\":[";
   for(int i=0;i<ArraySize(book)&&i<80;i++)
   {
      if(i>0)body+=",";
      body+="{\"book_type\":"+IntegerToString((int)book[i].type)+
            ",\"price\":"+DoubleToString(book[i].price,_Digits)+
            ",\"volume_real\":"+DoubleToString(book[i].volume_real,8)+
            ",\"volume\":"+IntegerToString((long)book[i].volume)+"}";
   }
   body+="]}";
   if(Append(h,body))g_depth_count++;
   FileFlush(h);FileClose(h);
}
void ExportHeartbeat()
{
   int h=OpenAppend();
   if(h==INVALID_HANDLE)return;
   string body="{"+Context()+
      "\"record_type\":\"STATUS\","+
      "\"time_msc\":"+IntegerToString((long)TimeGMT()*1000)+","+
      "\"print_records\":"+IntegerToString(g_print_count)+","+
      "\"quote_records\":"+IntegerToString(g_quote_count)+","+
      "\"dom_records\":"+IntegerToString(g_depth_count)+","+
      "\"dropped_records\":"+IntegerToString(g_drop_count)+","+
      "\"dom_subscribed\":"+(g_book_on?"true":"false")+"}";
   Append(h,body);FileFlush(h);FileClose(h);
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
   if(symbol!=g_symbol||!g_book_on)return;
   datetime now=TimeGMT();
   if(now-g_book_last<5)return;
   ExportDepth();g_book_last=now;
}
void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_book_on)MarketBookRelease(g_symbol);
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
