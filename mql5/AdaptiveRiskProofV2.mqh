// ABUTRON Option C V2: native, nonce-bound entry-risk evidence for the
// EXISTING Deriv and FP Markets order-bridge EAs.
// No order sender here. Only the original bridge may call OrderSend.
// Include in both existing EAs AFTER gNonce/gLogin/gServer and DIR_BASE.
#ifndef __ABUTRON_ADAPTIVE_PROOF_V2__
#define __ABUTRON_ADAPTIVE_PROOF_V2__

bool OptionCFinitePositive(double value)
{
  return MathIsValidNumber(value) && value > 0.0;
}

// Durable BEFORE sending. If this cannot be written, BLOCK the new order.
// It intentionally never contains credentials or passwords.
bool OptionCWriteIntent(const string key,const string sym,const string side,
                        const double equity,const double budget,
                        const double lots,const double intended_entry,
                        const double stop,const double target)
{
  if(StringLen(key)!=64 || !OptionCFinitePositive(equity) ||
     !OptionCFinitePositive(budget) || budget>equity*0.001+0.00001 ||
     !OptionCFinitePositive(lots) || !OptionCFinitePositive(intended_entry) ||
     !OptionCFinitePositive(stop) || !OptionCFinitePositive(target))return false;
  FolderCreate(DIR_BASE+"optionc_v2");
  string file=DIR_BASE+"optionc_v2\\"+key+".txt";
  if(FileIsExist(file))return false; // UNKNOWN is never automatically retried
  int h=FileOpen(file,FILE_WRITE|FILE_TXT|FILE_ANSI,0,CP_UTF8);
  if(h==INVALID_HANDLE)return false;
  string body="APEX_OPTIONC_NATIVE_RISK_INTENT_V2\r\n"+key+"\r\n"+
      gNonce+"\r\n"+gLogin+"\r\n"+gServer+"\r\n"+
      sym+"\r\n"+side+"\r\n"+
      DoubleToString(equity,2)+"\r\n"+
      DoubleToString(budget,4)+"\r\n"+
      DoubleToString(lots,8)+"\r\n"+
      DoubleToString(intended_entry,10)+"\r\n"+
      DoubleToString(stop,10)+"\r\n"+
      DoubleToString(target,10)+"\r\n"+
      IntegerToString((long)TimeGMT())+"\r\n";
  FileWriteString(h,body);FileFlush(h);FileClose(h);
  return FileIsExist(file);
}

// Issue #44: original Option C entries remain attributable after a BROKER-
// CONFIRMED, risk-reducing native trailing stop. This branch is enabled only
// by the FP account-bound bridge; other Option C users remain unchanged.
#ifdef ABUTRON_FP_NATIVE_TRAILING_COMPAT
bool OptionCNativeTrailProofForCurrentPosition(const string sym,
                                                const string side,
                                                const double vol,
                                                const double entry,
                                                const double originalStop,
                                                const double nowStop,
                                                const double nowTarget)
{
 string name="";
 long search=FileFindFirst(DIR_BASE+"trailing_proofs\\*.txt",name);
 if(search==INVALID_HANDLE)return false;
 bool attested=false;
 int seen=0;
 do
 {
  if(++seen>500)break; // excessive ambiguous proofs fail closed
  int dot=StringFind(name,".");
  if(dot<1||StringLen(name)<dot+12||
     StringSubstr(name,StringLen(name)-4)!=".txt")continue;
  ulong ticket=(ulong)StringToInteger(StringSubstr(name,0,dot));
  if(ticket==0||!PositionSelectByTicket(ticket)||
     PositionGetInteger(POSITION_MAGIC)!=MAGIC||
     PositionGetString(POSITION_SYMBOL)!=sym||
     PositionGetDouble(POSITION_VOLUME)!=vol||
     MathAbs(PositionGetDouble(POSITION_PRICE_OPEN)-entry)>0.00000001||
     MathAbs(PositionGetDouble(POSITION_SL)-nowStop)>0.00000001||
     MathAbs(PositionGetDouble(POSITION_TP)-nowTarget)>0.00000001)
     continue;
  long actualSide=PositionGetInteger(POSITION_TYPE);
  if(!((side=="BUY"&&actualSide==POSITION_TYPE_BUY)||
       (side=="SELL"&&actualSide==POSITION_TYPE_SELL)))continue;
  string file=DIR_BASE+"trailing_proofs\\"+name;
  int h=FileOpen(file,FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ,0,CP_UTF8);
  if(h==INVALID_HANDLE)continue;
  string a[15];bool full=true;
  for(int j=0;j<15;j++)
  {
   if(FileIsEnding(h)){full=false;break;}
   a[j]=FileReadString(h);
  }
  if(!FileIsEnding(h))full=false;
  FileClose(h);
  if(!full||a[0]!="APEX_FP_BROKER_NATIVE_TRAIL_READBACK_V1"||
     a[1]!=gNonce||a[2]!=gLogin||a[3]!=gServer||
     (ulong)StringToInteger(a[4])!=ticket||
     a[6]!=sym||a[7]!=side||
     a[12]!=IntegerToString((long)TRADE_RETCODE_DONE)||
     a[14]!="BROKER_SLTP_READBACK_CONFIRMED")continue;
  string bar=StringSubstr(name,dot+1,StringLen(name)-dot-5);
  if(a[5]!=bar)continue;
  double oldStop=StringToDouble(a[8]);
  double proposed=StringToDouble(a[9]);
  double observed=StringToDouble(a[10]);
  double target=StringToDouble(a[11]);
  if(!OptionCFinitePositive(oldStop)||!OptionCFinitePositive(proposed)||
     !OptionCFinitePositive(observed)||!OptionCFinitePositive(target)||
     MathAbs(proposed-nowStop)>0.00000001||
     MathAbs(observed-nowStop)>0.00000001||
     MathAbs(target-nowTarget)>0.00000001||
     (side=="BUY"&&!(originalStop<=oldStop&&oldStop<proposed))||
     (side=="SELL"&&!(originalStop>=oldStop&&oldStop>proposed)))continue;
  // Require the original immutable native intent and its broker result.
  string paths[2]={DIR_BASE+"trailing_intents\\"+name,
                    DIR_BASE+"trailing_results\\"+name};
  int lens[2]={15,12};
  string intent[15];string result[12];bool linked=true;
  for(int which=0;which<2;which++)
  {
   int fh=FileOpen(paths[which],FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ,0,CP_UTF8);
   if(fh==INVALID_HANDLE){linked=false;break;}
   for(int j=0;j<lens[which];j++)
   {
    if(FileIsEnding(fh)){linked=false;break;}
    if(which==0)intent[j]=FileReadString(fh);
    else result[j]=FileReadString(fh);
   }
   if(!FileIsEnding(fh))linked=false;
   FileClose(fh);
   if(!linked)break;
  }
  if(!linked||intent[0]!="APEX_FP_NATIVE_TRAIL_INTENT_V1"||
     result[0]!="APEX_FP_NATIVE_TRAIL_RESULT_V1"||
     intent[1]!=gNonce||result[1]!=gNonce||
     intent[2]!=gLogin||result[2]!=gLogin||
     intent[3]!=gServer||result[3]!=gServer||
     intent[4]!=a[4]||result[4]!=a[4]||
     intent[5]!=IntegerToString((long)PositionGetInteger(POSITION_IDENTIFIER))||
     intent[6]!=sym||intent[7]!=side||
     intent[8]!=a[5]||result[5]!=a[5]||
     intent[9]!=a[8]||result[8]!=a[8]||
     intent[10]!=a[9]||result[9]!=a[9]||
     intent[11]!=a[11]||result[10]!=a[11]||
     MathAbs(StringToDouble(intent[12])-vol)>0.00000001||
     (StringToInteger(intent[13])!=0&&
      (long)StringToInteger(intent[13])!=(long)TRADE_RETCODE_DONE)||
     result[6]!="1"||
     (long)StringToInteger(result[7])!=(long)TRADE_RETCODE_DONE)
     continue;
  attested=true;break;
 }while(FileFindNext(search,name));
 FileFindClose(search);
 return attested;
}
#endif

// Called ONLY from the existing EA's UpdateEvidenceFor(key,...), on a
// broker-visible position. This supports an adaptive position without
// mislabelling it a fixed-$50/$100 position. Still require independent
// broker-position and deal-history reconciliation on the Python side.
bool OptionCVerifyOpenPosition(const string key,const string sym,
                               const string side,const double vol,
                               const double entry,const double stop,
                               const double target,double &cashRisk,
                               double &cashTarget)
{
  string file=DIR_BASE+"optionc_v2\\"+key+".txt";
  if(!FileIsExist(file))return false;
  int h=FileOpen(file,FILE_READ|FILE_TXT|FILE_ANSI,0,CP_UTF8);
  if(h==INVALID_HANDLE)return false;
  string protocol=FileReadString(h),id=FileReadString(h);
  string nonce=FileReadString(h),login=FileReadString(h),server=FileReadString(h);
  string savedSymbol=FileReadString(h),savedSide=FileReadString(h);
  double originalEquity=StringToDouble(FileReadString(h));
  double budget=StringToDouble(FileReadString(h));
  double originalLots=StringToDouble(FileReadString(h));
  double intendedEntry=StringToDouble(FileReadString(h));
  double originalStop=StringToDouble(FileReadString(h));
  double originalTarget=StringToDouble(FileReadString(h));
  long originalTime=(long)StringToInteger(FileReadString(h));
  FileClose(h);
  if(protocol!="APEX_OPTIONC_NATIVE_RISK_INTENT_V2" || id!=key ||
     nonce!=gNonce || login!=gLogin || server!=gServer ||
     sym!=savedSymbol || side!=savedSide ||
     !OptionCFinitePositive(originalEquity) ||
     !OptionCFinitePositive(budget) ||
     !OptionCFinitePositive(originalLots) ||
     !OptionCFinitePositive(originalStop) ||
     !OptionCFinitePositive(originalTarget) ||
     originalTime<=0 || originalTime>(long)TimeGMT()+5 ||
     budget>originalEquity*0.001+0.00001 ||
     MathAbs(vol-originalLots)>0.0000001 ||
     MathAbs(target-originalTarget)>0.0000001)
    return false;
  if(!((side=="BUY" && originalStop<entry && entry<originalTarget &&
          stop<target && stop>=originalStop-0.00000001) ||
       (side=="SELL" && originalTarget<entry && entry<originalStop &&
          target<stop && stop<=originalStop+0.00000001)))return false;
  ENUM_ORDER_TYPE typ=(side=="BUY"?ORDER_TYPE_BUY:ORDER_TYPE_SELL);
  double originalLoss=0.0,observedStopProfit=0.0,reward=0.0;
  if(!OrderCalcProfit(typ,sym,vol,entry,originalStop,originalLoss) ||
     !OrderCalcProfit(typ,sym,vol,entry,stop,observedStopProfit) ||
     !OrderCalcProfit(typ,sym,vol,entry,target,reward))return false;
  double initialRisk=-originalLoss;
  if(!MathIsValidNumber(initialRisk)||initialRisk<=0||
     initialRisk>budget+0.005||!MathIsValidNumber(reward)||
     reward<1.5*initialRisk||
     !MathIsValidNumber(observedStopProfit)||
     observedStopProfit<originalLoss-0.001)return false;
#ifdef ABUTRON_FP_NATIVE_TRAILING_COMPAT
  // Never treat a changed stop as native Option C proof unless original
  // immutable SLTP intent, confirmed result and native broker readback match.
  if(MathAbs(stop-originalStop)>0.00000001 &&
     !OptionCNativeTrailProofForCurrentPosition(sym,side,vol,entry,
                                                originalStop,stop,target))
    return false;
#else
  // Other brokers have not opted in to the Chandelier provenance contract.
  if(MathAbs(stop-originalStop)>0.00000001)return false;
#endif
  cashRisk=MathMax(0.0,-observedStopProfit);
  cashTarget=reward;
  return MathIsValidNumber(cashRisk)&&cashRisk<=budget+0.005;
}
// Used inside the ORIGINAL PortfolioCanAdd to keep mixed legacy/V2 open
// positions accepted only when the broker-native V2 intent is verifiable.
bool OptionCAnyVerifiedPosition(const string sym,const string side,
                                const double volume,const double entry,
                                const double stop,const double target)
{
  string file="";
  long search=FileFindFirst(DIR_BASE+"optionc_v2\\*.txt",file);
  if(search==INVALID_HANDLE)return false;
  bool valid=false;
  int inspected=0;
  do
  {
    // Read all native intents so positions older than file 250 remain
    // attributable; there is no submission in this read-only lookup.
    if(++inspected>100000)break;
    int n=StringLen(file);
    if(n!=68 || StringSubstr(file,64)!=".txt")continue;
    string key=StringSubstr(file,0,64);
    double cashLoss=0.0,cashTarget=0.0;
    if(OptionCVerifyOpenPosition(key,sym,side,volume,entry,stop,target,
                                 cashLoss,cashTarget))
    { valid=true;break; }
  } while(FileFindNext(search,file));
  FileFindClose(search);
  return valid;
}
#endif