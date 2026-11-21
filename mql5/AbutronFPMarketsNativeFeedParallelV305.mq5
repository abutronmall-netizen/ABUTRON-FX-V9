// ABUTRON FP Markets broker-native DEMO/LIVE data-only Expert Advisor. NO ORDER FUNCTIONS.
// Isolated from the production Deriv native EA and Deriv execution/risk engine.
#property strict
#property version "3.06"
const string TARGETS[13]={"XAUUSD","BTCUSD","EURUSD","GBPUSD","USDJPY","AUDUSD",
 "US100","US30","SP500","GER40","USOIL","USDCHF","NZDUSD"};
const string ALIASES[13]={"XAUUSD.r|XAUUSD|GOLD","BTCUSD","EURUSD.r","GBPUSD.r","USDJPY.r","AUDUSD.r",
 "US100|USTEC|NAS100","US30|DJ30","US500|SP500|SPX500",
 "GER40|DE40|DAX40","XTIUSD","USDCHF.r","NZDUSD.r"};
const ENUM_TIMEFRAMES FRAMES[6]={PERIOD_M1,PERIOD_M5,PERIOD_M15,PERIOD_H1,PERIOD_H4,PERIOD_D1};
const string FRAME_NAMES[6]={"M1","M5","M15","H1","H4","D1"};
bool Hex32(const string s){
 if(StringLen(s)!=32)return false;
 for(int i=0;i<32;i++){ushort c=StringGetCharacter(s,i);
  if(!((c>=48&&c<=57)||(c>=97&&c<=102)))return false;}
 return true;
}
bool ServerOK(const string broker,const string server,const string mode){
 return broker=="FP_MARKETS" &&
  ((mode=="DEMO" && (server=="FPMarketsSC-Demo" || server=="FPMarketsSC-Demo Server" || server=="FPMarketsLtd-Demo2")) ||
   (mode=="LIVE" && (server=="FPMarketsSC-Live" || server=="FPMarketsLtd-Live2")));
}
bool AccountOK(const string server,const string login,const string mode){
 string company=AccountInfoString(ACCOUNT_COMPANY);StringToLower(company);
 return TerminalInfoInteger(TERMINAL_CONNECTED) &&
  AccountInfoInteger(ACCOUNT_LOGIN)==StringToInteger(login) &&
  AccountInfoInteger(ACCOUNT_TRADE_MODE)==(mode=="LIVE"?ACCOUNT_TRADE_MODE_REAL:ACCOUNT_TRADE_MODE_DEMO) &&
  AccountInfoString(ACCOUNT_SERVER)==server &&
  (StringFind(company,"fp markets")>=0 || StringFind(company,"fpmarkets")>=0 ||
   company=="first prudential markets limited");
}
string Clean(const string raw_text){
 string s=raw_text;
 StringReplace(s,"\t"," ");StringReplace(s,"\r"," ");
 StringReplace(s,"\n"," ");return s;
}
string Resolve(const int index,bool &ambiguous){
 ambiguous=false;string parts[];
 int n=StringSplit(ALIASES[index],StringGetCharacter("|",0),parts);
 string hit="";bool custom=false;
 for(int j=0;j<n;j++){
  if(SymbolExist(parts[j],custom)){
   if(hit!="" && hit!=parts[j]){ambiguous=true;return "";}
   hit=parts[j];
  }
 }
 return hit;
}
string Px(const double v,const int digits=8){
 if(!MathIsValidNumber(v))return "nan";
 return DoubleToString(v,digits);
}
void Catalog(const string nonce,const string owner,const string server,const string login,const string mode){
 int f=FileOpen("ABUTRON_FP_CATALOG_V305_"+nonce+".txt",
  FILE_WRITE|FILE_TXT|FILE_ANSI);
 if(f==INVALID_HANDLE)return;
 int total=SymbolsTotal(false);
 FileWriteString(f,"ABUTRON_FP_CATALOG_V1\n"+nonce+"\n"+owner+"\n"+
  server+"\n"+login+"\n"+mode+"\n"+IntegerToString((long)TimeGMT())+"\n"+
  IntegerToString(total)+"\n");
 // Full broker catalogue names: not an assumed fixed synthetic-asset list.
 int count=0;
 for(int i=0;i<total && i<25000;i++){
  string symbol=SymbolName(i,false);
  if(symbol=="")continue;
  string description=Clean(SymbolInfoString(symbol,SYMBOL_DESCRIPTION));
  FileWriteString(f,"S\t"+Clean(symbol)+"\t"+description+"\n");count++;
 }
 for(int i=0;i<13;i++){
  bool ambiguous=false;string symbol=Resolve(i,ambiguous);
  string status=(ambiguous?"AMBIGUOUS":(symbol==""?"MISSING":"MAPPED"));
  if(symbol!=""&&!ambiguous)SymbolSelect(symbol,true);
  long trade_mode=(symbol!=""&&!ambiguous)?
   SymbolInfoInteger(symbol,SYMBOL_TRADE_MODE):0;
  double contract=(symbol!=""&&!ambiguous)?
   SymbolInfoDouble(symbol,SYMBOL_TRADE_CONTRACT_SIZE):0;
  double minimum=(symbol!=""&&!ambiguous)?
   SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN):0;
  double step=(symbol!=""&&!ambiguous)?
   SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP):0;
  int digits=(symbol!=""&&!ambiguous)?
   (int)SymbolInfoInteger(symbol,SYMBOL_DIGITS):0;
  FileWriteString(f,"M\t"+TARGETS[i]+"\t"+symbol+"\t"+status+"\t"+
   IntegerToString(trade_mode)+"\t"+Px(contract)+"\t"+Px(minimum)+"\t"+
   Px(step)+"\t"+IntegerToString(digits)+"\n");
 }
 FileWriteString(f,"END\t"+IntegerToString(count)+"\t13\n");
 FileFlush(f);FileClose(f);
}
void Bars(const string nonce,const string owner,const string server,const string login,const string mode){
 int f=FileOpen("ABUTRON_FP_BARS_V305_"+nonce+".txt",
   FILE_WRITE|FILE_TXT|FILE_ANSI);
 if(f==INVALID_HANDLE)return;
 FileWriteString(f,"ABUTRON_FP_BARS_V1\n"+nonce+"\n"+owner+"\n"+server+
  "\n"+login+"\n"+mode+"\n"+IntegerToString((long)TimeGMT())+"\n");
 int nrows=0;
 for(int i=0;i<13 && !IsStopped();i++){
  bool ambiguous=false;string symbol=Resolve(i,ambiguous);
  if(ambiguous || symbol=="")continue;
  if(!SymbolSelect(symbol,true))continue;
  for(int j=0;j<6 && !IsStopped();j++){
   if(!AccountOK(server,login,mode))break;
   
   MqlRates rates[];ArraySetAsSeries(rates,false);
   int count=CopyRates(symbol,FRAMES[j],1,70,rates);
   if(count<1)continue;
   for(int k=0;k<count;k++){
    FileWriteString(f,TARGETS[i]+"\t"+symbol+"\t"+FRAME_NAMES[j]+"\t"+
     IntegerToString((long)rates[k].time)+"\t"+
     Px(rates[k].open)+"\t"+Px(rates[k].high)+"\t"+
     Px(rates[k].low)+"\t"+Px(rates[k].close)+"\n");
    nrows++;
   }
  }
 }
 FileWriteString(f,"END\t"+IntegerToString(nrows)+"\n");
 FileFlush(f);FileClose(f);
}
void Quotes(const string nonce,const string owner,const string server,const string login,const string mode){
 int f=FileOpen("ABUTRON_FP_QUOTES_V305_"+nonce+".txt",
   FILE_WRITE|FILE_TXT|FILE_ANSI);
 if(f==INVALID_HANDLE)return;
 FileWriteString(f,"ABUTRON_FP_QUOTES_V1\n"+nonce+"\n"+owner+"\n"+
   server+"\n"+login+"\n"+mode+"\n"+IntegerToString((long)TimeGMT())+"\n");
 int count=0;
 for(int i=0;i<13;i++){
  bool ambiguous=false;string symbol=Resolve(i,ambiguous);
  if(ambiguous||symbol==""||!SymbolSelect(symbol,true))continue;
  MqlTick tick;
  if(!SymbolInfoTick(symbol,tick))continue;
  FileWriteString(f,TARGETS[i]+"\t"+symbol+"\t"+
   IntegerToString((long)tick.time)+"\t"+Px(tick.bid)+
   "\t"+Px(tick.ask)+"\n");
  count++;
 }
 FileWriteString(f,"END\t"+IntegerToString(count)+"\n");
 FileFlush(f);FileClose(f);
}
// --- FP Markets V9 broker-native execution cost (data only; no order functions) ---
// This report measures an actual negative stop P/L with OrderCalcProfit for
// each of the thirteen mapped symbols. It never submits or modifies orders.
datetime FP_COST_LAST=0;

string FPJsonEscape(string value)
{
 StringReplace(value,"\\","\\\\");
 StringReplace(value,"\"","\\\"");
 StringReplace(value,"\r"," ");
 StringReplace(value,"\n"," ");
 StringReplace(value,"\t"," ");
 return value;
}

void PublishV9Cost(const string nonce,const string owner,
                   const string server,const string login,const string mode)
{
 // The source also handles LIVE feeds, but this export is exclusively
 // for the separately staged FP DEMO 0512 V9 cost adapter.
 if(mode!="DEMO" || !AccountOK(server,login,mode))return;
 const string prefix="ABUTRON_FP_COST_V1_"+nonce+".json";
 const string temporary=prefix+".tmp";
 bool all_verified=true;
 int valid_count=0;
 double equity=AccountInfoDouble(ACCOUNT_EQUITY);
 string currency=AccountInfoString(ACCOUNT_CURRENCY);
 // MqlTick.time is broker-server time; TimeGMT is UTC.
 // Measure both clocks from this same MT5 terminal, never assume GMT+2.
 long sample_utc=(long)TimeGMT();
 long broker_epoch=(long)TimeTradeServer();
 if(sample_utc<=0 || broker_epoch<=0)all_verified=false;
 if(!MathIsValidNumber(equity) || equity<=0.0 || currency!="USD")
    all_verified=false;

 string instruments="";
 for(int i=0;i<13 && !IsStopped();i++)
 {
   bool ambiguous=false;
   string sym=Resolve(i,ambiguous);
   if(ambiguous || sym=="" || !SymbolSelect(sym,true))
   {
      all_verified=false;
      continue;
   }
   MqlTick tick;
   double step=SymbolInfoDouble(sym,SYMBOL_VOLUME_STEP);
   double minimum=SymbolInfoDouble(sym,SYMBOL_VOLUME_MIN);
   double maximum=SymbolInfoDouble(sym,SYMBOL_VOLUME_MAX);
   double tick_size=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_SIZE);
   double point=SymbolInfoDouble(sym,SYMBOL_POINT);
   double contract=SymbolInfoDouble(sym,SYMBOL_TRADE_CONTRACT_SIZE);
   double value_profit=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE_PROFIT);
   double value_loss=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE_LOSS);
   long trade_mode=SymbolInfoInteger(sym,SYMBOL_TRADE_MODE);
   long stops=SymbolInfoInteger(sym,SYMBOL_TRADE_STOPS_LEVEL);
   long freeze=SymbolInfoInteger(sym,SYMBOL_TRADE_FREEZE_LEVEL);
   int digits=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
   if(!SymbolInfoTick(sym,tick) || tick.bid<=0 || tick.ask<=tick.bid ||
      MathAbs((double)(broker_epoch-(long)tick.time))>90.0 ||
      tick_size<=0 || point<=0 || contract<=0 ||
      minimum<=0 || step<=0 || maximum<minimum ||
      trade_mode!=SYMBOL_TRADE_MODE_FULL)
   {
      all_verified=false;
      continue;
   }
   double buy_stop=NormalizeDouble(tick.ask-10.0*tick_size,digits);
   double sell_stop=NormalizeDouble(tick.bid+10.0*tick_size,digits);
   double buy_loss=0.0,sell_loss=0.0;
   if(buy_stop<=0.0 || sell_stop<=tick.bid ||
      !OrderCalcProfit(ORDER_TYPE_BUY,sym,minimum,tick.ask,buy_stop,buy_loss) ||
      !OrderCalcProfit(ORDER_TYPE_SELL,sym,minimum,tick.bid,sell_stop,sell_loss) ||
      !MathIsValidNumber(buy_loss) || !MathIsValidNumber(sell_loss) ||
      buy_loss>=0.0 || sell_loss>=0.0)
   {
      all_verified=false;
      continue;
   }
   if(valid_count>0)instruments+=",";
   instruments+="\""+FPJsonEscape(sym)+"\":{"
     +"\"symbol\":\""+FPJsonEscape(sym)+"\","
     +"\"tick_size\":"+Px(tick_size,8)+","
     +"\"point\":"+Px(point,8)+","
     +"\"contract_size\":"+Px(contract,8)+","
     +"\"volume_min\":"+Px(minimum,8)+","
     +"\"volume_step\":"+Px(step,8)+","
     +"\"volume_max\":"+Px(maximum,8)+","
     +"\"tick_value_profit\":"+Px(value_profit,8)+","
     +"\"tick_value_loss\":"+Px(value_loss,8)+","
     +"\"stops_level\":"+IntegerToString(stops)+","
     +"\"freeze_level\":"+IntegerToString(freeze)+","
     +"\"digits\":"+IntegerToString(digits)+","
     +"\"trade_mode\":"+IntegerToString(trade_mode)+","
     +"\"bid\":"+Px(tick.bid,8)+","
     +"\"ask\":"+Px(tick.ask,8)+","
     +"\"tick_epoch\":"+IntegerToString((long)tick.time)+","
     +"\"probe_ticks\":10,"
     +"\"buy_probe_native_ordercalcprofit_usd\":"+Px(buy_loss,8)+","
     +"\"sell_probe_native_ordercalcprofit_usd\":"+Px(sell_loss,8)
     +"}";
   valid_count++;
 }
 all_verified=(all_verified && valid_count==13);
 string suffix=StringSubstr(login,StringLen(login)-4);
 string json="{\"schema\":\"abutron.fpmarkets.native.cost_snapshot.v1\","
   +"\"source\":\"MT5_OrderCalcProfit\","
   +"\"nonce\":\""+FPJsonEscape(nonce)+"\","
   +"\"heartbeat_owner\":\""+FPJsonEscape(owner)+"\","
   +"\"broker_server\":\""+FPJsonEscape(server)+"\","
   +"\"mt5_login\":\""+FPJsonEscape(login)+"\","
   +"\"account_suffix\":\""+FPJsonEscape(suffix)+"\","
   +"\"environment\":\"DEMO\","
   +"\"sample_epoch\":"+IntegerToString(sample_utc)+","
   +"\"broker_server_epoch\":"+IntegerToString(broker_epoch)+","
   +"\"broker_clock_offset_seconds\":"+IntegerToString(broker_epoch-sample_utc)+","
   +"\"account_currency\":\""+FPJsonEscape(currency)+"\","
   +"\"account_equity_usd\":"+Px(equity,2)+","
   +"\"native_bid_ask_and_ordercalcprofit_verified\":"
   +(all_verified?"true":"false")+","
   +"\"verified_instrument_count\":"+IntegerToString(valid_count)+","
   +"\"expected_instrument_count\":13,"
   +"\"instruments\":{"+instruments+"}}";
 int handle=FileOpen(temporary,FILE_WRITE|FILE_TXT|FILE_ANSI,0,CP_UTF8);
 if(handle==INVALID_HANDLE)
 {
   Print("ABUTRON_FP_V9_COST_OPEN_FAILED code=",GetLastError());
   return;
 }
 uint written=FileWriteString(handle,json);
 FileFlush(handle);FileClose(handle);
 if(written==0 || !FileMove(temporary,0,prefix,FILE_REWRITE))
    Print("ABUTRON_FP_V9_COST_PUBLISH_FAILED code=",GetLastError());
}

// Per-account state remains inside the isolated terminal; no shared globals across clients.
string FP_NONCE="",FP_OWNER="",FP_SERVER="",FP_LOGIN="",FP_MODE="";
bool FP_READY=false;
datetime FP_LAST=0;
bool FP_ANNOUNCED_OFFLINE=false;
// Read-only broker-history indicator attachment. In this isolated terminal
// profile, MT5 discards indicator entries during chart initialization.
// Attach the ALREADY signed indicator only after authenticated MT5 proof.
// P1 fix: the history indicator is configurable so replacing V307 with V305
// preserves the existing account-specific V101 history functionality instead
// of auto-attaching the older V100 indicator.
//
// Path correction (974fc55 review): the V101 binary on Ubuntu is installed at
//   ABUTRON/ABUTRON-FP-HISTORY-V101-READONLY/AbutronFPAccountHistoryV101.ex5
// so the iCustom() path must include the subdirectory. The full relative
// path is configurable via InpHistoryIndicatorDir + InpHistoryIndicator.
input string InpHistoryIndicator = "AbutronFPAccountHistoryV101";
input string InpHistoryIndicatorDir = "ABUTRON\\ABUTRON-FP-HISTORY-V101-READONLY";
bool FP_HISTORY_INSTALLED=false;
int FP_HISTORY_HANDLE=INVALID_HANDLE;
datetime FP_HISTORY_ATTEMPT_TIME=0;
void EnsureFPHistoryIndicator()
{
 if(FP_HISTORY_INSTALLED)return;
 if(TimeLocal()-FP_HISTORY_ATTEMPT_TIME<30)return;
 FP_HISTORY_ATTEMPT_TIME=TimeLocal();
 // Don't attach a duplicate when MT5 already restored the indicator.
 for(int i=0;i<ChartIndicatorsTotal(0,0);i++)
 {
  string name=ChartIndicatorName(0,0,i);
  if(StringFind(name,InpHistoryIndicator)>=0)
  {
   FP_HISTORY_INSTALLED=true;
   Print("ABUTRON_FP_HISTORY_INDICATOR_ALREADY_ATTACHED");
   return;
  }
 }
 ResetLastError();
 string hist_path=InpHistoryIndicatorDir+"\\"+InpHistoryIndicator;
 int handle=iCustom(_Symbol,_Period,hist_path);
 if(handle==INVALID_HANDLE)
 {
  Print("ABUTRON_FP_HISTORY_INDICATOR_CREATE_FAILED code=",GetLastError()," path=",hist_path);
  return;
 }
 ResetLastError();
 if(!ChartIndicatorAdd(0,0,handle))
 {
  Print("ABUTRON_FP_HISTORY_INDICATOR_ATTACH_FAILED code=",GetLastError());
  IndicatorRelease(handle);
  return;
 }
 FP_HISTORY_HANDLE=handle;
 FP_HISTORY_INSTALLED=true;
 Print("ABUTRON_FP_HISTORY_INDICATOR_ATTACHED_AFTER_BROKER_AUTH: "+hist_path);
}

// V3 market-microstructure indicator attachment. ApexMarketFeedV3 is a
// custom indicator (OnCalculate) that runs alongside the chart's EA.
// MT5 discards indicator entries during chart initialization in this
// isolated profile, so attach the ALREADY signed indicator only after
// authenticated MT5 proof, using the same iCustom()/ChartIndicatorAdd()
// mechanism as the V101 history indicator.
//
// The V3 indicator EX5 is staged by stage_v3_indicator_binary() into
// MQL5/Indicators/ABUTRON/ApexMarketFeedV3.ex5. The full relative path
// is configurable via InpV3IndicatorDir + InpV3Indicator.
input string InpV3Indicator = "ApexMarketFeedV3";
input string InpV3IndicatorDir = "ABUTRON";
bool FP_V3_INSTALLED=false;
int FP_V3_HANDLE=INVALID_HANDLE;
datetime FP_V3_ATTEMPT_TIME=0;
void EnsureV3Indicator()
{
 if(FP_V3_INSTALLED)return;
 if(TimeLocal()-FP_V3_ATTEMPT_TIME<30)return;
 FP_V3_ATTEMPT_TIME=TimeLocal();
 // Don't attach a duplicate when MT5 already restored the indicator.
 for(int i=0;i<ChartIndicatorsTotal(0,0);i++)
 {
  string name=ChartIndicatorName(0,0,i);
  if(StringFind(name,InpV3Indicator)>=0)
  {
   FP_V3_INSTALLED=true;
   Print("ABUTRON_FP_V3_INDICATOR_ALREADY_ATTACHED");
   return;
  }
 }
 ResetLastError();
 string v3_path=InpV3IndicatorDir+"\\"+InpV3Indicator;
 int handle=iCustom(_Symbol,_Period,v3_path);
 if(handle==INVALID_HANDLE)
 {
  Print("ABUTRON_FP_V3_INDICATOR_CREATE_FAILED code=",GetLastError()," path=",v3_path);
  return;
 }
 ResetLastError();
 if(!ChartIndicatorAdd(0,0,handle))
 {
  Print("ABUTRON_FP_V3_INDICATOR_ATTACH_FAILED code=",GetLastError());
  IndicatorRelease(handle);
  return;
 }
 FP_V3_HANDLE=handle;
 FP_V3_INSTALLED=true;
 Print("ABUTRON_FP_V3_INDICATOR_ATTACHED_AFTER_BROKER_AUTH: "+v3_path);
}


// Bind only to the authenticated running FP DEMO MT5 feed heartbeat.
// No one-time challenge reuse, no password and no heartbeat writer.
bool FreshSession(const bool initial=false)
{
 string path="",other="";
 long found=FileFindFirst("ABUTRON_SESSION_HEARTBEAT_V1_*.txt",path);
 if(found==INVALID_HANDLE)return false;
 bool duplicate=FileFindNext(found,other);
 FileFindClose(found);
 if(duplicate)return false;
 long modified=FileGetInteger(path,FILE_MODIFY_DATE);
 if(modified<=0 || MathAbs((double)((long)TimeGMT()-modified))>25.0)return false;
 int f=FileOpen(path,FILE_READ|FILE_TXT|FILE_ANSI,0,CP_UTF8);
 if(f==INVALID_HANDLE)return false;
 string schema=FileReadString(f),nonce=FileReadString(f);
 string owner=FileReadString(f),broker=FileReadString(f);
 string server=FileReadString(f),mode=FileReadString(f);
 string status=FileReadString(f),orders=FileReadString(f);
 FileClose(f);
 if(schema!="ABUTRON_MT5_NATIVE_HEARTBEAT_V1"||!Hex32(nonce)||
   path!="ABUTRON_SESSION_HEARTBEAT_V1_"+nonce+".txt"||
   StringLen(owner)!=36||broker!="FP_MARKETS"||
   server!="FPMarketsSC-Demo"||mode!="DEMO"||
   status!="CONNECTED"||orders!="NO_ORDER_FUNCTIONS")return false;
 if(initial)
 {
   FP_NONCE=nonce;FP_OWNER=owner;FP_SERVER=server;
   FP_LOGIN=IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN));
   FP_MODE=mode;
 }
 return nonce==FP_NONCE && owner==FP_OWNER &&
   server==FP_SERVER && mode==FP_MODE &&
   AccountOK(FP_SERVER,FP_LOGIN,FP_MODE);
}
int OnInit()
{
 Print("ABUTRON_FP_V305_READONLY_PARALLEL_INITIALIZING");
 // Profile EAs may precede the authenticated startup feed. Remain inert
 // until the broker-bound heartbeat is valid, then publish read-only data.
 if(!EventSetTimer(3))
 {
   Print("ABUTRON_FP_V305_TIMER_FAILED ",GetLastError());
   return INIT_FAILED;
 }
 if(FreshSession(true))
 {
   FP_READY=true;
   Print("ABUTRON_FP_V305_READONLY_PARALLEL_SESSION_VERIFIED");
 }
 else Print("ABUTRON_FP_V305_WAITING_FOR_AUTHENTICATED_SESSION");
 return INIT_SUCCEEDED;
}
void OnTimer(){
 if(!FP_READY)
 {
   if(!FreshSession(true))return;
   FP_READY=true;
   Print("ABUTRON_FP_V305_READONLY_PARALLEL_SESSION_RECOVERED");
 }
 bool verified=FreshSession();
 
 if(!verified){
   if(!FP_ANNOUNCED_OFFLINE){
     // One-shot diagnostic with boolean checks only; never print account login or password.
     string actual_company=AccountInfoString(ACCOUNT_COMPANY);
     StringToLower(actual_company);
     bool company_ok=(StringFind(actual_company,"fp markets")>=0 ||
        StringFind(actual_company,"fpmarkets")>=0 ||
        actual_company=="first prudential markets limited");
     Print("ABUTRON_FP_ACCOUNT_ATTESTATION connected=",TerminalInfoInteger(TERMINAL_CONNECTED),
        " login_match=",(AccountInfoInteger(ACCOUNT_LOGIN)==StringToInteger(FP_LOGIN)),
        " server_match=",(AccountInfoString(ACCOUNT_SERVER)==FP_SERVER),
        " mode_match=",(AccountInfoInteger(ACCOUNT_TRADE_MODE)==(FP_MODE=="LIVE"?ACCOUNT_TRADE_MODE_REAL:ACCOUNT_TRADE_MODE_DEMO)),
        " company_match=",company_ok);
     Print("ABUTRON_FP_DATA_EA_BROKER_NOT_AUTHENTICATED mode=",FP_MODE);
     FP_ANNOUNCED_OFFLINE=true;
   }
   return;
 }
 if(FP_ANNOUNCED_OFFLINE){
   Print("ABUTRON_FP_DATA_EA_BROKER_AUTHENTICATED");
   FP_ANNOUNCED_OFFLINE=false;
 }
 EnsureFPHistoryIndicator();
 EnsureV3Indicator();
 // Separate 20s cost refresh maintains the consumer's 65s freshness.
 // Existing catalog/quote/bars cadence is unchanged.
 if(FP_MODE=="DEMO" && TimeLocal()-FP_COST_LAST>=20){
   PublishV9Cost(FP_NONCE,FP_OWNER,FP_SERVER,FP_LOGIN,FP_MODE);
   FP_COST_LAST=TimeLocal();
 }
 if(TimeLocal()-FP_LAST>=60){
   Catalog(FP_NONCE,FP_OWNER,FP_SERVER,FP_LOGIN,FP_MODE);
   Quotes(FP_NONCE,FP_OWNER,FP_SERVER,FP_LOGIN,FP_MODE);
   Bars(FP_NONCE,FP_OWNER,FP_SERVER,FP_LOGIN,FP_MODE);
   FP_LAST=TimeLocal();
 }
}
void OnDeinit(const int reason){
 EventKillTimer();
 Print("ABUTRON_FP_DATA_EA_STOP reason=",reason);
}
