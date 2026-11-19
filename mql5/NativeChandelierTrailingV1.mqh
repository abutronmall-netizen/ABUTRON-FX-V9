// ABUTRON Issue #44: FP Markets DEMO-only native Chandelier stop amendment.
// This file is included after account/nonce, risk and receipt helpers.
// The *existing* EA remains the sole broker sender. Default trading input OFF.
// No fresh entries, no second trade bridge, no LIVE account support.
#define DIR_TRAIL_INTENTS "ABUTRON_FP\\trailing_intents\\"
#define DIR_TRAIL_RESULTS "ABUTRON_FP\\trailing_results\\"
#define DIR_TRAIL_PROOFS "ABUTRON_FP\\trailing_proofs\\"

bool NativeTrailSaveOnce(string path,string body)
{
 if(FileIsExist(path) || StringLen(body)<30 || StringLen(body)>4000)return false;
 int h=FileOpen(path,FILE_WRITE|FILE_TXT|FILE_ANSI,0,CP_UTF8);
 if(h==INVALID_HANDLE)return false;
 int wrote=(int)FileWriteString(h,body);
 FileFlush(h);FileClose(h);
 // A partial original is never retried or mistaken for a durable intent.
 return wrote==StringLen(body);
}

bool NativeTrailCandidate(string symbol,bool buy,double volume,double entry,
                          double oldStop,double oldTake,
                          long &closedBar,double &candidate)
{
 if(volume<=0||entry<=0||oldStop<=0||oldTake<=0)return false;
 if(!((buy&&oldStop<entry&&entry<oldTake)||
      (!buy&&oldTake<entry&&entry<oldStop)||
      (buy&&oldStop>=entry&&oldStop<oldTake)||
      (!buy&&oldTake<oldStop&&oldStop<=entry)))return false;
 if(SymbolInfoInteger(symbol,SYMBOL_TRADE_MODE)!=SYMBOL_TRADE_MODE_FULL)return false;
 MqlTick tick;
 if(!SymbolInfoTick(symbol,tick)||tick.bid<=0||tick.ask<tick.bid||
    (long)tick.time<=0||MathAbs((double)(TimeCurrent()-(long)tick.time))>20)return false;
 MqlRates bars[30];
 if(CopyRates(symbol,PERIOD_M5,1,30,bars)!=30)return false;
 closedBar=(long)bars[29].time;
 if(closedBar<=0||closedBar%300!=0||
    TimeCurrent()-closedBar>610||closedBar>TimeCurrent())return false;
 double atrSum=0.0,highest=0.0,lowest=1e100;
 for(int i=1;i<30;i++)
 {
  if(bars[i].high<=0||bars[i].low<=0||bars[i].high<bars[i].low||
     bars[i-1].close<=0)return false;
  if(i>=16)
  {
   double tr=MathMax(bars[i].high-bars[i].low,
      MathMax(MathAbs(bars[i].high-bars[i-1].close),
              MathAbs(bars[i].low-bars[i-1].close)));
   atrSum+=tr;
  }
  if(i>=8){highest=MathMax(highest,bars[i].high);lowest=MathMin(lowest,bars[i].low);}
 }
 double atr=atrSum/14.0;
 if(!MathIsValidNumber(atr)||atr<=0||lowest<=0||highest<=lowest)return false;
 double ticksize=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE);
 double point=SymbolInfoDouble(symbol,SYMBOL_POINT);
 if(ticksize<=0||point<=0)return false;
 int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
 double quoteClose=buy?tick.bid:tick.ask;
 ENUM_ORDER_TYPE side=buy?ORDER_TYPE_BUY:ORDER_TYPE_SELL;
 double floating=0.0,oldStopProfit=0.0;
 if(!OrderCalcProfit(side,symbol,volume,entry,quoteClose,floating)||
    !OrderCalcProfit(side,symbol,volume,entry,oldStop,oldStopProfit)||
    !MathIsValidNumber(floating)||!MathIsValidNumber(oldStopProfit)||
    floating<=0||floating<MathMax(MathAbs(oldStopProfit),0.0001*AccountInfoDouble(ACCOUNT_EQUITY)))
    return false;
 // Only trailing the stop when profit is already at least 1R.
 // Conservative break-even cap: a new stop is never worse than entry.
 double raw=buy?MathMax(highest-3.0*atr,entry+ticksize):
                MathMin(lowest+3.0*atr,entry-ticksize);
 candidate=NormalizeDouble((buy?MathFloor(raw/ticksize+1e-8):
                                 MathCeil(raw/ticksize-1e-8))*ticksize,digits);
 if(!MathIsValidNumber(candidate)||candidate<=0)return false;
 // Strict monotonicity: BUY moves UP; SELL moves DOWN, by >= 2 ticks.
 if((buy&&candidate<oldStop+2*ticksize-1e-8)||
    (!buy&&candidate>oldStop-2*ticksize+1e-8))return false;
 double minDistance=MathMax((double)SymbolInfoInteger(symbol,SYMBOL_TRADE_STOPS_LEVEL),
                            (double)SymbolInfoInteger(symbol,SYMBOL_TRADE_FREEZE_LEVEL))*point;
 // Existing stop must also be outside the broker freeze band.
 if(MathAbs(quoteClose-oldStop)<=minDistance+ticksize)return false;
 if(buy && !(candidate<quoteClose-minDistance-ticksize&&candidate<oldTake))return false;
 if(!buy && !(candidate>quoteClose+minDistance+ticksize&&candidate>oldTake))return false;
 double improvedStopProfit=0.0;
 if(!OrderCalcProfit(side,symbol,volume,entry,candidate,improvedStopProfit)||
    !MathIsValidNumber(improvedStopProfit)||
    improvedStopProfit<=oldStopProfit+0.001)return false;
 return true;
}

// Proven broker result must be matched to the exact account, position, old
// and proposed stop, and original TP. A local OrderSend ACK is NOT a proof.
bool NativeTrailReadBack(string stem)
{
 string result=DIR_TRAIL_RESULTS+stem+".txt";
 string proof=DIR_TRAIL_PROOFS+stem+".txt";
 if(FileIsExist(proof)||!FileIsExist(result))return FileIsExist(proof);
 int h=FileOpen(result,FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ,0,CP_UTF8);
 if(h==INVALID_HANDLE)return false;
 string schema=FileReadString(h),nonce=FileReadString(h);
 string login=FileReadString(h),server=FileReadString(h);
 string ticketStr=FileReadString(h),barStr=FileReadString(h);
 string sent=FileReadString(h),retcodeStr=FileReadString(h);
 string oldStr=FileReadString(h),newStr=FileReadString(h),tpStr=FileReadString(h);
 string observedUtc=FileReadString(h);
 bool extra=!FileIsEnding(h);
 FileClose(h);
 if(extra||schema!="APEX_FP_NATIVE_TRAIL_RESULT_V1"||nonce!=gNonce||
    login!=gLogin||server!=gServer||sent!="1"||
    (long)StringToInteger(retcodeStr)!=(long)TRADE_RETCODE_DONE)return false;
 // Cross-bind to the immutable original intent before accepting broker ACK.
 // Result alone must not create a proof of a stop amendment.
 string original=DIR_TRAIL_INTENTS+stem+".txt";
 int oh=FileOpen(original,FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ,0,CP_UTF8);
 if(oh==INVALID_HANDLE)return false;
 string intent[15];
 bool complete=true;
 for(int j=0;j<15;j++)
 {
  if(FileIsEnding(oh)){complete=false;break;}
  intent[j]=FileReadString(oh);
 }
 if(!FileIsEnding(oh))complete=false;
 FileClose(oh);
 if(!complete||intent[0]!="APEX_FP_NATIVE_TRAIL_INTENT_V1"||
    intent[1]!=nonce||intent[2]!=login||intent[3]!=server||
    intent[4]!=ticketStr||intent[8]!=barStr||intent[9]!=oldStr||
    intent[10]!=newStr||intent[11]!=tpStr||
    (StringToInteger(intent[13])!=0&&
     (long)StringToInteger(intent[13])!=(long)TRADE_RETCODE_DONE))return false;
 ulong ticket=(ulong)StringToInteger(ticketStr);
 if(ticket==0||!PositionSelectByTicket(ticket)||
    PositionGetInteger(POSITION_MAGIC)!=MAGIC||
    PositionGetInteger(POSITION_IDENTIFIER)!=(long)StringToInteger(intent[5]))return false;
 string symbol=PositionGetString(POSITION_SYMBOL);
 if(!ApprovedSymbol(symbol)||intent[6]!=symbol)return false;
 double oldStop=StringToDouble(oldStr),newStop=StringToDouble(newStr);
 double tp=StringToDouble(tpStr),nowSL=PositionGetDouble(POSITION_SL);
 double nowTP=PositionGetDouble(POSITION_TP);
 long side=PositionGetInteger(POSITION_TYPE);
 if(side!=POSITION_TYPE_BUY&&side!=POSITION_TYPE_SELL)return false;
 bool buy=side==POSITION_TYPE_BUY;
 if(intent[7]!=(buy?"BUY":"SELL")||
    StringToDouble(intent[12])<PositionGetDouble(POSITION_VOLUME)-0.0000001)return false;
 double ticksize=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE);
 if(ticksize<=0||oldStop<=0||newStop<=0||tp<=0||
    MathAbs(nowSL-newStop)>ticksize*0.51||
    MathAbs(nowTP-tp)>ticksize*0.51||
    (buy&&newStop<=oldStop)||( !buy&&newStop>=oldStop))return false;
 string body="APEX_FP_BROKER_NATIVE_TRAIL_READBACK_V1\r\n"+
   gNonce+"\r\n"+gLogin+"\r\n"+gServer+"\r\n"+
   ticketStr+"\r\n"+barStr+"\r\n"+symbol+"\r\n"+
   (buy?"BUY":"SELL")+"\r\n"+oldStr+"\r\n"+newStr+"\r\n"+
   DoubleToString(nowSL,10)+"\r\n"+DoubleToString(nowTP,10)+"\r\n"+
   retcodeStr+"\r\n"+IntegerToString((long)TimeGMT())+"\r\n"+
   "BROKER_SLTP_READBACK_CONFIRMED\r\n";
 return NativeTrailSaveOnce(proof,body);
}

// Any uncertain native SLTP submission blocks further amendments; on the
// next tick reconcile the original result through actual broker position.
// A failed, missing or ambiguous result is never blindly resubmitted.
bool NativeTrailPendingOrUnverified()
{
 string name="";
 long ff=FileFindFirst(DIR_TRAIL_INTENTS+"*.txt",name);
 if(ff==INVALID_HANDLE)return false;
 bool pending=false;
 int checked=0;
 do
 {
  checked++;
  if(checked>500){pending=true;break;}
  if(StringLen(name)<6||StringSubstr(name,StringLen(name)-4)!=".txt")
  {pending=true;continue;}
  string stem=StringSubstr(name,0,StringLen(name)-4);
  if(!FileIsExist(DIR_TRAIL_PROOFS+name))
  {
   if(!NativeTrailReadBack(stem))pending=true;
  }
 } while(FileFindNext(ff,name));
 FileFindClose(ff);
 return pending;
}

// At most ONE protective-stop amendment per verified M5 bar/timer cycle.
// No new order or position is opened by this routine.
void NativeChandelierTrailing()
{
 if(!EnableNativeChandelierTrailing||gNonce==""||!VerifiedDemoAccount()||
    !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)||
    !MQLInfoInteger(MQL_TRADE_ALLOWED)||
    !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))return;
 if(NativeTrailPendingOrUnverified())return;
 if(OrdersTotal()!=0||PositionsTotal()>13)return;
 FolderCreate("ABUTRON_FP");
 FolderCreate("ABUTRON_FP\\trailing_intents");
 FolderCreate("ABUTRON_FP\\trailing_results");
 FolderCreate("ABUTRON_FP\\trailing_proofs");
 for(int i=0;i<PositionsTotal();i++)
 {
  ulong ticket=PositionGetTicket(i);
  if(ticket==0||PositionGetInteger(POSITION_MAGIC)!=MAGIC)continue;
  string symbol=PositionGetString(POSITION_SYMBOL);
  if(!ApprovedSymbol(symbol))continue;
  long side=PositionGetInteger(POSITION_TYPE);
  if(side!=POSITION_TYPE_BUY&&side!=POSITION_TYPE_SELL)continue;
  bool buy=(side==POSITION_TYPE_BUY);
  double qty=PositionGetDouble(POSITION_VOLUME);
  double entry=PositionGetDouble(POSITION_PRICE_OPEN);
  double oldStop=PositionGetDouble(POSITION_SL);
  double oldTake=PositionGetDouble(POSITION_TP);
  long closedBar=0;
  double trail=0.0;
  if(!NativeTrailCandidate(symbol,buy,qty,entry,oldStop,oldTake,closedBar,trail))
      continue;
  string stem=IntegerToString((long)ticket)+"."+IntegerToString(closedBar);
  if(FileIsExist(DIR_TRAIL_INTENTS+stem+".txt"))continue;
  MqlTradeRequest req={};MqlTradeResult res={};MqlTradeCheckResult check={};
  req.action=TRADE_ACTION_SLTP;
  req.position=ticket;
  req.symbol=symbol;
  req.sl=trail;
  req.tp=oldTake; // Keep original TP, and no additional entry.
  req.magic=MAGIC;
  ResetLastError();
  bool ok=OrderCheck(req,check);
  if(!ok||(check.retcode!=0&&check.retcode!=TRADE_RETCODE_DONE))continue;
  // Re-select immediately before the durable intent and broker submission.
  // Reject a position whose ticket, magic, SL or TP changed during OrderCheck.
  if(!PositionSelectByTicket(ticket)||
     PositionGetInteger(POSITION_MAGIC)!=MAGIC||
     PositionGetString(POSITION_SYMBOL)!=symbol||
     PositionGetInteger(POSITION_TYPE)!=side||
     MathAbs(PositionGetDouble(POSITION_SL)-oldStop)>0.00000001||
     MathAbs(PositionGetDouble(POSITION_TP)-oldTake)>0.00000001||
     MathAbs(PositionGetDouble(POSITION_VOLUME)-qty)>0.00000001)continue;
  // Binding: account session, broker, ticket, original stop, proposed stop,
  // exact closed bar, exact TP. Intent must be durable BEFORE OrderSend.
  string oldText=DoubleToString(oldStop,10);
  string trailText=DoubleToString(trail,10);
  string tpText=DoubleToString(oldTake,10);
  string ticketText=IntegerToString((long)ticket);
  string barText=IntegerToString(closedBar);
  string intent="APEX_FP_NATIVE_TRAIL_INTENT_V1\r\n"+
      gNonce+"\r\n"+gLogin+"\r\n"+gServer+"\r\n"+
      ticketText+"\r\n"+
      IntegerToString((long)PositionGetInteger(POSITION_IDENTIFIER))+"\r\n"+
      symbol+"\r\n"+(buy?"BUY":"SELL")+"\r\n"+
      barText+"\r\n"+oldText+"\r\n"+trailText+"\r\n"+
      tpText+"\r\n"+DoubleToString(qty,8)+"\r\n"+
      IntegerToString((long)check.retcode)+"\r\n"+
      IntegerToString((long)TimeGMT())+"\r\n";
  if(!NativeTrailSaveOnce(DIR_TRAIL_INTENTS+stem+".txt",intent))return;
  // Exactly one OrderSend. A native retcode without a broker SL/TP
  // read-back never constitutes a confirmed trailing modification.
  bool sent=OrderSend(req,res);
  string result="APEX_FP_NATIVE_TRAIL_RESULT_V1\r\n"+
      gNonce+"\r\n"+gLogin+"\r\n"+gServer+"\r\n"+
      ticketText+"\r\n"+barText+"\r\n"+(sent?"1":"0")+"\r\n"+
      IntegerToString((long)res.retcode)+"\r\n"+
      oldText+"\r\n"+trailText+"\r\n"+tpText+"\r\n"+
      IntegerToString((long)TimeGMT())+"\r\n";
  if(!NativeTrailSaveOnce(DIR_TRAIL_RESULTS+stem+".txt",result))return;
  NativeTrailReadBack(stem);
  return;
 }
}