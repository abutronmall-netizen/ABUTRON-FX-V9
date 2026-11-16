#property copyright "ABUTRON FX TEST ONLY"
#property version "1.000"
// No order sends; public test vectors only.
void OnStart()
{
   // A test harness: execute only in a DEMO terminal, never a LIVE account.
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   {
      Print("ABUTRON_PR18_HMAC_SKIPPED_NON_DEMO");
      return;
   }
   int out = FileOpen("ABUTRON_PR18_HMAC_KAT_V1.txt",
                      FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(out == INVALID_HANDLE) { Print("HMAC_KAT_OPEN_FAILED ", GetLastError()); return; }
   int failed = 0;
   string computed_0 = HmacSha256Hex("key", "The quick brown fox jumps over the lazy dog");
   bool ok_0 = computed_0 == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8";
   FileWriteString(out, "rfc4231-ish" + "|" + (ok_0 ? "PASS" : "FAIL") +
                   "|" + computed_0 + "\n");
   if(!ok_0) failed++;
   string computed_1 = HmacSha256Hex("kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk", "ABUTRON_REQ_demo|cand|cid|EURUSD.r|BUY|0.050000|1.085000|1.090000|CONFIRM|1.086500|2.000000|1.084000");
   bool ok_1 = computed_1 == "778986646c5c0481245bda8b1184801934beefd2e78470ad64a8111232492662";
   FileWriteString(out, "long-key-kkkkkkkkkkkkkkkk" + "|" + (ok_1 ? "PASS" : "FAIL") +
                   "|" + computed_1 + "\n");
   if(!ok_1) failed++;
   string computed_2 = HmacSha256Hex("clé-安全", "nonce|EURUSD.r|SELL|π");
   bool ok_2 = computed_2 == "1fb50011299db8a824a7b72b3ca789d5b9e5304786779ea58c87c1a47b042189";
   FileWriteString(out, "utf8" + "|" + (ok_2 ? "PASS" : "FAIL") +
                   "|" + computed_2 + "\n");
   if(!ok_2) failed++;
   FileClose(out);
   Print("ABUTRON_PR18_HMAC_NATIVE_CASES ", failed == 0 ? "PASS" : "FAIL");
   // Return from the script; do NOT stop or restart the MT5 terminal.
}

string HmacSha256Hex(string key, string message)
{
   // Convert key to byte array
   ushort keyBytes[];
   uchar keyUtf8[];
   int keyCount = StringToCharArray(key, keyUtf8, 0, WHOLE_ARRAY, CP_UTF8);
   if(keyCount <= 0) return("");
   int keyLen = keyCount - 1; // exclude trailing NUL
   ArrayResize(keyBytes, keyLen);
   for(int i = 0; i < keyLen; i++)
      keyBytes[i] = (ushort)keyUtf8[i];

   // If key > 64 bytes, hash it first
   if(keyLen > 64)
   {
      string keyHex = Sha256Hex(keyBytes, keyLen);
      ushort hashedBytes[];
      HexToBytes(keyHex, hashedBytes);
      ArrayCopy(keyBytes, hashedBytes, 0, 0, ArraySize(hashedBytes));
      ArrayResize(keyBytes, ArraySize(hashedBytes));
      keyLen = ArraySize(keyBytes);
   }

   // Pad key to 64 bytes
   ArrayResize(keyBytes, 64);
   for(int i = keyLen; i < 64; i++)
      keyBytes[i] = 0;

   // Build ipad and opad
   ushort ipad[];
   ushort opad[];
   ArrayResize(ipad, 64);
   ArrayResize(opad, 64);
   for(int i = 0; i < 64; i++)
   {
      ipad[i] = (ushort)(keyBytes[i] ^ 0x36);
      opad[i] = (ushort)(keyBytes[i] ^ 0x5c);
   }

   // Convert message to byte array
   ushort msgBytes[];
   uchar messageUtf8[];
   int msgCount = StringToCharArray(message, messageUtf8, 0, WHOLE_ARRAY, CP_UTF8);
   if(msgCount <= 0) return("");
   int msgLen = msgCount - 1; // exclude trailing NUL
   ArrayResize(msgBytes, msgLen);
   for(int i = 0; i < msgLen; i++)
      msgBytes[i] = (ushort)messageUtf8[i];

   // Inner hash: H(ipad || message)
   ushort innerInput[];
   ArrayResize(innerInput, 64 + msgLen);
   for(int i = 0; i < 64; i++)
      innerInput[i] = ipad[i];
   for(int i = 0; i < msgLen; i++)
      innerInput[64 + i] = msgBytes[i];
   string innerHex = Sha256Hex(innerInput, 64 + msgLen);
   ushort innerBytes[];
   HexToBytes(innerHex, innerBytes);
   int innerLen = ArraySize(innerBytes);

   // Outer hash: H(opad || inner)
   ushort outerInput[];
   ArrayResize(outerInput, 64 + innerLen);
   for(int i = 0; i < 64; i++)
      outerInput[i] = opad[i];
   for(int i = 0; i < innerLen; i++)
      outerInput[64 + i] = innerBytes[i];
   string result = Sha256Hex(outerInput, 64 + innerLen);
   return(result);
}

//+------------------------------------------------------------------+
//| SHA-256 (uses uint for 32-bit words — valid MQL5 type)            |
//+------------------------------------------------------------------+
string Sha256Hex(const ushort &inputBytes[], int inputLen)
{
   if(inputLen < 0 || inputLen > ArraySize(inputBytes)) return("");
   uchar data[], emptyKey[], digest[];
   ArrayResize(data, inputLen);
   for(int i = 0; i < inputLen; i++)
      data[i] = (uchar)(inputBytes[i] & 0xFF);
   int n = CryptEncode(CRYPT_HASH_SHA256, data, emptyKey, digest);
   if(n != 32) return("");
   string hex = "";
   for(int j = 0; j < 32; j++)
      hex += StringFormat("%02x", (uint)digest[j]);
   return(hex);
}

//+------------------------------------------------------------------+
//| Convert hex string to byte array (reference param — MQL5 cannot  |
//| return arrays from functions)                                   |
//+------------------------------------------------------------------+
void HexToBytes(string hex, ushort &bytes[])
{
   int len = StringLen(hex) / 2;
   ArrayResize(bytes, len);
   for(int i = 0; i < len; i++)
   {
      string hb = StringSubstr(hex, i * 2, 2);
      int hi = HexVal(StringGetCharacter(hb, 0));
      int lo = HexVal(StringGetCharacter(hb, 1));
      bytes[i] = (ushort)((hi << 4) | lo);
   }
}

int HexVal(ushort c)
{
   if(c >= '0' && c <= '9') return((int)c - 48);
   if(c >= 'a' && c <= 'f') return((int)c - 97 + 10);
   if(c >= 'A' && c <= 'F') return((int)c - 65 + 10);
   return(0);
}

