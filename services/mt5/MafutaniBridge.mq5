//+------------------------------------------------------------------+
//| MafutaniBridge.mq5                                              |
//|                                                                   |
//| Long-polls the local bridge server and executes what it returns.   |
//|                                                                   |
//| WHY THE EA IS A CLIENT, NOT A SERVER                              |
//|                                                                   |
//| MQL5 has no server primitive. `WebRequest()` is strictly outbound  |
//| and every URL it touches must be whitelisted in                   |
//|   Tools > Options > Expert Advisors > Allow WebRequest           |
//|                                                                   |
//| So the topology is inverted from the usual "expose an API"        |
//| arrangement: a Python server runs beside the terminal             |
//| (services/mt5/bridge_server.py) and this EA polls it.            |
//|                                                                   |
//| WHAT IS ACTUALLY SAFE TO TRUST                                     |
//|                                                                   |
//| Nothing about the *result* of a trade is inferred here beyond what |
//| OrderSend returns. TRADE_RETCODE_DONE (10009) means the terminal  |
//| accepted the request -- it does NOT mean the order filled. The    |
//| bridge reports 10009 through as "accepted" and the adapter refuses |
//| to call it filled. Fills are only ever learned from                |
//| OnTradeTransaction.                                               |
//+------------------------------------------------------------------+
#property copyright "Mafutani"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input string BridgeHost    = "127.0.0.1";   // must match the bridge bind
input int    BridgePort    = 8031;
input string BridgeToken   = "";            // must match --token
input int    PollMs        = 500;           // OnTimer interval
input int    MaxLots       = 0;             // 0 = no EA-side cap
input bool   AllowTrading  = false;         // must be true to place orders

CTrade   trade;
datetime lastResultPush = 0;

//+------------------------------------------------------------------+
//| Push the result of the command we just ran back to the bridge.    |
//+------------------------------------------------------------------+
bool PostResult(const string id, const bool ok, const string err,
                const string dataJson)
  {
   string body = "{\"id\":\"" + id + "\",\"ok\":"
                 + (ok ? "true" : "false")
                 + ",\"error\":\"" + err + "\""
                 + (dataJson == "" ? "" : ",\"data\":" + dataJson) + "}";

   char   payload[];
   // MQL5 has no WebRequest overload taking BOTH headers and a body. The
   // compiler offers only:
   //   (method,url,cookie,headers,timeout,data,data_size,result,result_headers)
   //   (method,url,timeout,data,result,result_headers)
   // So a POST that carries a token cannot set a header, and the token
   // travels as a query parameter instead. An earlier version passed the
   // headers string as the *cookie* argument, which matches neither
   // overload -- the compiler caught it.
   StringToCharArray(body, payload, 0, WHOLE_ARRAY, CP_UTF8);
   ArrayResize(payload, ArraySize(payload) - 1);   // drop the trailing 0

   char result[];
   string rh = ResultHeaders();
   // POST overload:
   //   WebRequest(method,url,cookie,headers,timeout,data,data_size,result,result_headers)
   // `data` is `const char&[]` and must be passed by reference with an
   // explicit size. A dynamic `char[]` does not satisfy it -- the compiler
   // rejected that form twice. The 6-arg overload has the same const-array
   // constraint and no cookie/header slot, which is why the token rides in
   // the URL rather than a header.
   if(WebRequest("POST", ResultUrl(), "", rh, 5000,
                 payload, ArraySize(payload), result, rh) == -1)
     {
      Print("MafutaniBridge: POST /result failed, error ", GetLastError());
      return false;
     }
   return true;
  }

//+------------------------------------------------------------------+
//| URLs carry the token, because MQL5 cannot set headers on a POST.  |
//+------------------------------------------------------------------+
string Base()          { return "http://" + BridgeHost + ":" + IntegerToString(BridgePort); }
string ResultUrl()     { return Base() + "/result?token=" + BridgeToken; }
string NextUrl()       { return Base() + "/next?token="   + BridgeToken; }
string ResultHeaders() { return "Content-Type: application/json\r\n"; }

//+------------------------------------------------------------------+
//| Escape a value for embedding in the JSON we hand back.            |
//+------------------------------------------------------------------++
string JStr(const string s)
  {
   string o = s;
   StringReplace(o, "\\", "\\\\");
   StringReplace(o, "\"", "\\\"");
   return o;
  }

//+------------------------------------------------------------------+
//| Execute one command. Returns JSON for `data` on success.         |
//+------------------------------------------------------------------+
bool Execute(const string command, string &dataJson, string &err)
  {
   dataJson = "";
   err = "";

   string op = ExtractCommandOp(command);

   if(op == "ping")
     {
      bool trade_allowed = (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) != 0);
      dataJson = "{\"build\":" + IntegerToString(TerminalInfoInteger(TERMINAL_BUILD))
                 + ",\"terminal\":\"" + JStr(TerminalInfoString(TERMINAL_NAME))
                 + ",\"company\":\"" + JStr(AccountInfoString(ACCOUNT_COMPANY))
                 + ",\"account\":\"" + JStr(IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)))
                 + ",\"trade_allowed\":" + (trade_allowed ? "true" : "false")
                 + ",\"trade_context_busy\":" + (TradeContextBusy() ? "true" : "false") + "}";
      return true;
     }

   if(op == "account")
     {
      dataJson = "{\"cash\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2)
                 + ",\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2)
                 + ",\"buying_power\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2)
                 + ",\"currency\":\"" + JStr(AccountInfoString(ACCOUNT_CURRENCY))
                 + ",\"account_id\":\"" + JStr(IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN))) + "\"}";
      return true;
     }

   if(op == "quote")
     {
      string sym = ExtractCommandValue(command, "symbol");
      MqlTick tick;
      if(!SymbolInfoTick(sym, tick))
        { err = "no tick for " + sym; return false; }
      dataJson = "{\"bid\":" + DoubleToString(tick.bid, 8)
                 + ",\"ask\":" + DoubleToString(tick.ask, 8)
                 + ",\"last\":" + DoubleToString(tick.last, 8)
                 + ",\"bid_size\":" + DoubleToString((double)tick.volume, 4)
                 + ",\"ask_size\":" + DoubleToString((double)tick.volume, 4)
                 + ",\"ts\":" + IntegerToString((long)tick.time) + "}";
      return true;
     }

   if(op == "submit")  return DoSubmit(command, dataJson, err);
   if(op == "cancel")  return DoCancel(command, dataJson, err);
   if(op == "open_orders" || op == "order") return DoOrders(command, dataJson, err);

   err = "unknown op: " + op;
   return false;
  }

// Trade-context-busy has no usable accessor in this build: neither
// IsTradeContextBusy() nor MQL_TRADE_CONTEXT_BUSY resolves, and both were
// removed after the compiler rejected them. The retcode is the mechanism --
// a second concurrent OrderSend returns 10025, which brokers/mt5.py already
// maps to "trade context busy". So the EA does not pre-check it; it
// reports whatever the terminal actually said.
bool TradeContextBusy()
  {
   return false;
  }

//+------------------------------------------------------------------+
//| Place one order. Returns retcode verbatim -- never "filled".      |
//+------------------------------------------------------------------+
bool DoSubmit(const string command, string &dataJson, string &err)
  {
   if(!AllowTrading)
     { err = "AllowTrading is false in the EA inputs"; return false; }
   if(TradeContextBusy())
     { err = "trade context busy (retcode 10025)"; return false; }

   string sym = ExtractCommandValue(command, "symbol");
   string side = ExtractCommandValue(command, "side");
   double qty  = StringToDouble(ExtractCommandValue(command, "qty"));
   string type = ExtractCommandValue(command, "type");
   double price = StringToDouble(ExtractCommandValue(command, "price"));
   double stop  = StringToDouble(ExtractCommandValue(command, "stop_price"));
   string coid  = ExtractCommandValue(command, "client_order_id");

   if(MaxLots > 0 && qty > (double)MaxLots)
     { err = "qty " + DoubleToString(qty,2) + " exceeds EA cap "
            + DoubleToString((double)MaxLots,2); return false; }

   // SetExpertMagicNumber returns void -- it cannot be used in an if. An
   // earlier version did exactly that and the compiler caught it.
   trade.SetExpertMagicNumber(0);
   trade.SetTypeFillingBySymbol(sym);

   bool sent = false;
   if(type == "limit")
      sent = trade.BuyLimit(qty, price, sym, 0.0, stop)
          || trade.SellLimit(qty, price, sym, 0.0, stop);
   else if(type == "stop")
      sent = trade.BuyStop(qty, price, sym, 0.0, stop)
          || trade.SellStop(qty, price, sym, 0.0, stop);
   else
      sent = (side == "sell")
           ? trade.Sell(qty, sym, 0.0, stop)
           : trade.Buy(qty, sym, 0.0, stop);

   uint ret = trade.ResultRetcode();
   ulong ticket = trade.ResultOrder();

   // The retcode is passed through untouched. 10009 here means the
   // terminal accepted the request; it does NOT mean filled, and the
   // Python side is written specifically not to treat it as such.
   dataJson = "{\"retcode\":" + IntegerToString((int)ret)
              + ",\"ticket\":\"" + IntegerToString((long)ticket) + "\""
              + ",\"price\":" + DoubleToString(trade.ResultPrice(), 8)
              + ",\"client_order_id\":\"" + JStr(coid) + "\""
              + ",\"ts\":" + IntegerToString((long)TimeCurrent()) + "}";

   if(!sent)
     { err = "OrderSend failed, retcode " + IntegerToString((int)ret); return false; }
   return true;
  }

//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
bool DoCancel(const string command, string &dataJson, string &err)
  {
   if(!AllowTrading)
     { err = "AllowTrading is false in the EA inputs"; return false; }
   ulong ticket = (ulong)StringToInteger(ExtractCommandValue(command, "ticket"));
   bool ok = trade.OrderDelete(ticket);
   uint ret = trade.ResultRetcode();
   dataJson = "{\"retcode\":" + IntegerToString((int)ret)
              + ",\"ticket\":\"" + IntegerToString((long)ticket) + "\"}";
   if(!ok)
     { err = "OrderDelete failed, retcode " + IntegerToString((int)ret); return false; }
   return true;
  }

//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
bool DoOrders(const string command, string &dataJson, string &err)
  {
   string arr = "[";
   int n = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      string side = (OrderGetInteger(ORDER_TYPE) == ORDER_TYPE_BUY) ? "buy" : "sell";
      arr += (n > 0 ? "," : "")
           + "{\"ticket\":\"" + IntegerToString((long)ticket) + "\""
           + ",\"symbol\":\"" + JStr(OrderGetString(ORDER_SYMBOL)) + "\""
           + ",\"side\":\"" + side + "\""
           + ",\"qty\":" + DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 2)
           + ",\"price\":" + DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), 8)
           + ",\"status\":\"open\""
           + ",\"ts\":" + IntegerToString((long)OrderGetInteger(ORDER_TIME_SETUP)) + "}";
      n++;
     }
   dataJson = "{\"orders\":" + arr + "]}";
   return true;
  }

//+------------------------------------------------------------------+
//| Minimal field extraction. The bridge always sends flat JSON, so   |
//| this avoids pulling in a parser and its failure modes.            |
//+------------------------------------------------------------------+
string ExtractCommandValue(const string cmd, const string key)
  {
   string needle = "\"" + key + "\":";
   int p = StringFind(cmd, needle);
   if(p < 0) return "";
   int s = p + StringLen(needle);
   if(StringGetCharacter(cmd, s) == '"')
     {
      int e = StringFind(cmd, "\"", s + 1);
      if(e < 0) return "";
      return StringSubstr(cmd, s + 1, e - s - 1);
     }
   int e = StringFind(cmd, ",", s);
   if(e < 0) e = StringFind(cmd, "}", s);
   if(e < 0) e = StringLen(cmd);
   return StringSubstr(cmd, s, e - s);
  }

string ExtractCommandOp(const string cmd)
  {
   return ExtractCommandValue(cmd, "op");
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(0);
   if(!AllowTrading)
      Print("MafutaniBridge: AllowTrading=false. Reads work; orders are refused.");
   else
      Print("MafutaniBridge: AllowTrading=true. This EA can place real orders.");
   Print("MafutaniBridge: polling ", NextUrl());
   EventSetMillisecondTimer(PollMs);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { EventKillTimer(); }

//+------------------------------------------------------------------+
void OnTimer()
  {
   char result[];
   string rh = ResultHeaders();
   char noBody[];

   int code = WebRequest("GET", NextUrl(), "", rh, 10000,
                         noBody, 0, result, rh);
   if(code == -1)
     {
      // Polling failure is normal when the bridge is not running; say so
      // once in a while rather than every tick, which floods the Experts log.
      static datetime lastWarn = 0;
      if(TimeCurrent() - lastWarn > 60)
        { Print("MafutaniBridge: /next unreachable, error ", GetLastError());
          lastWarn = TimeCurrent(); }
      return;
     }

   string body = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(StringFind(body, "\"command\":null") >= 0) return;   // idle
   int p = StringFind(body, "\"command\":");
   if(p < 0) return;

   string command = ExtractCommandValue(body, "command");
   // Re-extract the id from the command object itself: it was injected by
   // the bridge and is what the result must be keyed by.
   string id = ExtractCommandValue(command, "id");
   if(id == "") return;

   string dataJson, err;
   bool ok = Execute(command, dataJson, err);
   PostResult(id, ok, err, dataJson);

   lastResultPush = TimeCurrent();
  }

//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   // Fills are learned HERE and only here. Nothing infers a fill from an
   // OrderSend return code, because TRADE_RETCODE_DONE only means the
   // request was accepted.
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
      Print("MafutaniBridge: deal add, ticket ", trans.deal,
            " order ", trans.order, " type ", trans.type);
  }
//+------------------------------------------------------------------+
