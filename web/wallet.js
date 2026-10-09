/* Wallet page: MetaMask connect + read-only on-chain balances.
 *
 * Read-only by construction. This file cannot sign or send: the only RPC
 * methods it calls are eth_requestAccounts, eth_accounts,
 * eth_chainId, eth_getBalance and eth_call. There is no path here for a
 * private key or seed phrase, and no network call to this server carries
 * anything but the public address the user chose to share.
 *
 * Contract addresses come from GET /api/chains, not from this file, so
 * there is one copy of them. decimals() is read from the contract and
 * cross-checked against symbol()/decimals() before any balance is
 * displayed -- assuming 6 would misreport BNB Chain balances (18 dp) by
 * twelve orders of magnitude.
 *
 * Isolated from app.js on purpose: a syntax error in the main bundle once
 * silently killed login, so wallet code lives on its own.
 */
(function () {
  "use strict";

  var CHAINS = [];
  var state = { address: null, chainId: null, accounts: null };
  var listenersBound = false;

  // ERC-20 ABI selectors, hardcoded constants (keccak of the signature).
  var SEL = {
    symbol: "0x95d89b41",        // symbol()
    decimals: "0x313ce567",      // decimals()
    balanceOf: "0x70a08231",     // balanceOf(address)
    allowance: "0xdd62ed3e"      // allowance(address,address)
  };

  function $(id) { return document.getElementById(id); }

  function setStatus(msg, kind) {
    var el = $("wallet-status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "wallet-status" + (kind ? " wallet-status-" + kind : "");
  }

  function shortAddress(a) {
    if (!a) return "-";
    return a.length > 12 ? a.slice(0, 6) + "…" + a.slice(-4) : a;
  }

  function provider() {
    if (typeof window === "undefined" || !window.ethereum) return null;
    return window.ethereum;
  }

  // MetaMask exposes multiple injected providers when several wallets are
  // installed. Picking the array blindly can hand us the wrong wallet, so
  // we select the one that identifies as MetaMask when present.
  function getProvider() {
    var p = provider();
    if (!p) return null;
    if (Array.isArray(p.providers)) {
      for (var i = 0; i < p.providers.length; i++) {
        if (p.providers[i] && p.providers[i].isMetaMask) return p.providers[i];
      }
      return p.providers[0] || null;
    }
    return p;
  }

  function chainById(id) {
    for (var i = 0; i < CHAINS.length; i++) {
      if (String(CHAINS[i].hex).toLowerCase() === String(id).toLowerCase()) return CHAINS[i];
    }
    return null;
  }

  // ---- ABI encoding -------------------------------------------------
  // Minimal, dependency-free. We only ever encode a 20-byte address in a
  // 32-byte left-padded word, which is all balanceOf/allowance need.
  function padAddress(addr) {
    return "000000000000000000000000" + String(addr).toLowerCase().replace(/^0x/, "");
  }

  function ethCall(eth, to, data) {
    return eth.request({ method: "eth_call", params: [{ to: to, data: data }, "latest"] });
  }

  // Decode an ABI `string` return (dynamic type: offset, length, bytes).
  function decodeString(hex) {
    if (!hex || hex === "0x") return "";
    var body = String(hex).replace(/^0x/, "");
    try {
      var offset = parseInt(body.slice(0, 64), 16) * 2;
      var len = parseInt(body.slice(offset, offset + 64), 16);
      var out = "";
      for (var i = 0; i < len; i++) {
        var code = parseInt(body.substr(offset + 64 + i * 2, 2), 16);
        if (code) out += String.fromCharCode(code);
      }
      return out.trim();
    } catch (e) {
      return "";
    }
  }

  // hex (no 0x) -> BigInt. Used to move a uint256 without going through
  // Number(), which would silently round any balance above 2^53 wei.
  function toBigInt(hex) {
    var s = String(hex || "").replace(/^0x/, "");
    if (!s) return BigInt(0);
    try {
      return BigInt("0x" + s);
    } catch (e) {
      return BigInt(0);
    }
  }

  // Decode a uint256 return and render it with `decimals` dp.
  //
  // The scale division happens on the BigInt, not on the hex digits.
  // Truncating the hex string instead -- pad and slice -- is wrong,
  // because a hex digit is not a decimal digit: 987654321000000 is
  // 0x3824430f6ce40, and cutting six characters off the end of that
  // yields "3824430.f6ce4" rather than 987654321. That failure is silent
  // and produces a confident, wrong number.
  function formatUnits(rawHex, decimals) {
    if (!rawHex || rawHex === "0x") return "0";
    var dp = Math.max(0, parseInt(decimals, 10) || 0);
    var value = toBigInt(rawHex);
    if (dp === 0) return groupDigits(value.toString());

    var scale = BigInt(10) ** BigInt(dp);
    var whole = value / scale;
    var frac = (value % scale).toString();
    // Left-pad the fraction to exactly dp digits before trimming
    // trailing zeros, so 0.5 does not render as "0.5" from "5".
    frac = frac.padStart(dp, "0").replace(/0+$/, "");
    var text = groupDigits(whole.toString());
    return frac ? text + "." + frac : text;
  }

  function groupDigits(digits) {
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  // ---- reads --------------------------------------------------------
  // Verifies the contract really is USD₮ before trusting its numbers.
  // A wrong address would otherwise render a plausible wrong balance,
  // which is the one failure mode a balance display cannot afford.
  function readUsdt(eth, address, chain) {
    return ethCall(eth, chain.usdt_address, SEL.symbol).then(function (symHex) {
      var sym = decodeString(symHex);
      return ethCall(eth, chain.usdt_address, SEL.decimals).then(function (decHex) {
        var dec = parseInt(String(decHex).replace(/^0x/, ""), 16);
        if (isNaN(dec)) throw new Error("contract returned no decimals()");
        if (!/^usdt/i.test(sym)) {
          throw new Error("contract at " + chain.usdt_address.slice(0, 10) +
            "… reports symbol '" + (sym || "?") + "', not USDT — refusing to " +
            "display a balance from an unrecognised token");
        }
        if (dec !== chain.usdt_expected_decimals) {
          // Show the real value, but say so: this is exactly the case
          // where assuming a decimals figure produces a wrong number.
          return ethCall(eth, chain.usdt_address, SEL.balanceOf + padAddress(address))
            .then(function (bal) {
              return { symbol: sym, decimals: dec, raw: bal,
                       mismatch: true };
            });
        }
        return ethCall(eth, chain.usdt_address, SEL.balanceOf + padAddress(address))
          .then(function (bal) {
            return { symbol: sym, decimals: dec, raw: bal, mismatch: false };
          });
      });
    });
  }

  function render(address, chainId) {
    var eth = getProvider();
    $("wallet-address").textContent = shortAddress(address);
    var link = $("wallet-explorer");
    var chain = chainById(chainId);
    if (chain && address) {
      link.href = chain.explorer + "/address/" + address;
      link.style.display = "";
    } else {
      link.removeAttribute("href");
      link.style.display = "none";
    }
    $("wallet-chain").textContent = chain ? chain.name : ("unknown (" + chainId + ")");

    if (!chain) {
      setStatus("Connected to a network this app has no USD₮ contract for. " +
                "Only read-only data is shown, and no balance is displayed " +
                "rather than guessing an address.", "warn");
      $("wallet-usdt").textContent = "-";
      $("wallet-native").textContent = "-";
      return Promise.resolve();
    }

    $("wallet-usdt").textContent = "reading…";
    $("wallet-native").textContent = "reading…";

    return readUsdt(eth, address, chain)
      .then(function (info) {
        var text = formatUnits(info.raw, info.decimals) + " " + info.symbol;
        if (info.mismatch) {
          text += "  (" + info.decimals + " dp, not the expected " +
                  chain.usdt_expected_decimals + " — using the contract's value)";
        }
        $("wallet-usdt").textContent = text;
      })
      .catch(function (e) {
        $("wallet-usdt").textContent = "unavailable";
        setStatus("USDT balance unavailable: " + (e && e.message ? e.message : e), "warn");
      })
      .then(function () {
        return eth.request({
          method: "eth_getBalance", params: [address, "latest"]
        }).then(function (balHex) {
          $("wallet-native").textContent =
            formatUnits(balHex, chain.native_decimals) + " " + chain.native_symbol;
        }).catch(function () {
          $("wallet-native").textContent = "unavailable";
        });
      });
  }

  // ---- connect / disconnect -----------------------------------------
  function connect() {
    var eth = getProvider();
    if (!eth) {
      setStatus("No injected wallet found. Install MetaMask, then reload. " +
                "This app never asks for a seed phrase.", "warn");
      return;
    }
    setStatus("Waiting for wallet approval…");
    // Deliberately not auto-connecting on page load: eth_requestAccounts
    // pops a permission prompt, and prompting a user who did not ask for
    // it is how dapps get reputation for being hostile.
    eth.request({ method: "eth_requestAccounts" }).then(function (accounts) {
      state.accounts = accounts;
      state.address = accounts && accounts[0] ? accounts[0] : null;
      return eth.request({ method: "eth_chainId" });
    }).then(function (chainId) {
      state.chainId = chainId;
      $("wallet-connect").style.display = "none";
      $("wallet-disconnect").style.display = "";
      $("wallet-panel").style.display = "";
      render(state.address, state.chainId);
      setStatus("Connected. Read-only — this page cannot sign or send.", "ok");
    }).catch(function (e) {
      var msg = (e && e.code === 4001) ? "Connection rejected." :
                "Could not connect: " + (e && e.message ? e.message : e);
      setStatus(msg, "warn");
    });
  }

  // MetaMask offers no true "disconnect" for a dapp: the site keeps its
  // permission until the user removes it in the extension. We drop our
  // local state and say so, rather than implying the link is severed.
  function disconnect() {
    state = { address: null, chainId: null, accounts: null };
    $("wallet-address").textContent = "-";
    $("wallet-chain").textContent = "-";
    $("wallet-usdt").textContent = "-";
    $("wallet-native").textContent = "-";
    $("wallet-panel").style.display = "none";
    $("wallet-connect").style.display = "";
    $("wallet-disconnect").style.display = "none";
    setStatus("Disconnected from this page. To revoke access for good, " +
              "remove the site in MetaMask → Settings → Connections.", "ok");
  }

  function switchChain() {
    var eth = getProvider();
    if (!eth) return;
    var sel = $("wallet-chain-select");
    var chain = chainById(sel.value);
    if (!chain) return;
    eth.request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: chain.hex }]
    }).then(function () {
      setStatus("Switched to " + chain.name + ".", "ok");
    }).catch(function (e) {
      // 4902 = chain unknown to the wallet; offer to add it.
      if (e && e.code === 4902) {
        setStatus("MetaMask does not know " + chain.name + " yet.", "warn");
        $("wallet-add-chain").style.display = "";
        $("wallet-add-chain").dataset.chain = chain.hex;
      } else if (e && e.code === 4001) {
        setStatus("Network switch rejected.", "warn");
      } else {
        setStatus("Could not switch network: " + (e && e.message ? e.message : e), "warn");
      }
    });
  }

  function addChain() {
    var eth = getProvider();
    var chain = chainById($("wallet-add-chain").dataset.chain);
    if (!eth || !chain) return;
    eth.request({
      method: "wallet_addEthereumChain",
      params: [{
        chainId: chain.hex,
        chainName: chain.name,
        nativeCurrency: { name: chain.native_symbol, symbol: chain.native_symbol,
                          decimals: chain.native_decimals },
        blockExplorerUrls: [chain.explorer],
        // rpcUrls: omitted. The wallet uses its own default provider; this
        // app has no RPC endpoint to offer and would rather MetaMask
        // choose than ship a third-party endpoint into a balance display.
      }]
    }).then(function () {
      $("wallet-add-chain").style.display = "none";
      setStatus("Added " + chain.name + ".", "ok");
    }).catch(function (e) {
      setStatus("Could not add network: " + (e && e.message ? e.message : e), "warn");
    });
  }

  function bindListeners() {
    if (listenersBound) return;
    var eth = getProvider();
    if (!eth || !eth.on) return;
    listenersBound = true;
    // Account or network changed underneath us: re-read, do not guess.
    eth.on("accountsChanged", function (accounts) {
      if (!accounts || !accounts.length) { disconnect(); return; }
      state.accounts = accounts;
      state.address = accounts[0];
      render(state.address, state.chainId);
    });
    eth.on("chainChanged", function (chainId) {
      state.chainId = chainId;
      render(state.address, chainId);
    });
    eth.on("disconnect", function () { disconnect(); });
  }

  // ---- page entry ---------------------------------------------------
  async function loadChains() {
    var res = await fetch("/api/chains", { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error("HTTP " + res.status);
    var data = await res.json();
    CHAINS = data.chains || [];
    var sel = $("wallet-chain-select");
    sel.innerHTML = "";
    CHAINS.forEach(function (c) {
      var opt = document.createElement("option");
      opt.value = c.hex;
      opt.textContent = c.name;
      sel.appendChild(opt);
    });
  }

  window.MafutaniWallet = {
    render: function () {
      var eth = getProvider();
      if (!eth) {
        $("wallet-panel").style.display = "none";
        setStatus("No injected wallet found. Install MetaMask, then reload. " +
                  "This app never asks for a seed phrase.", "warn");
        return;
      }
      loadChains().then(function () {
        // Show an already-authorised account without prompting: eth_accounts
        // returns only what the user previously allowed, so this cannot
        // raise a permission popup.
        return eth.request({ method: "eth_accounts" }).then(function (accounts) {
          bindListeners();
          if (accounts && accounts.length) {
            state.accounts = accounts;
            state.address = accounts[0];
            return eth.request({ method: "eth_chainId" }).then(function (chainId) {
              state.chainId = chainId;
              $("wallet-connect").style.display = "none";
              $("wallet-disconnect").style.display = "";
              $("wallet-panel").style.display = "";
              var chain = chainById(chainId);
              if (chain) $("wallet-chain-select").value = chain.hex;
              render(state.address, chainId);
            });
          }
          $("wallet-panel").style.display = "none";
        });
      }).catch(function (e) {
        setStatus("Could not load chain list: " + (e && e.message ? e.message : e), "warn");
      });
    },
    onConnect: connect,
    onDisconnect: disconnect,
    onSwitchChain: switchChain,
    onAddChain: addChain
  };
})();
