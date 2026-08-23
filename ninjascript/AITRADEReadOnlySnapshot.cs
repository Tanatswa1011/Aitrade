// AITRADE Phase 54F — read-only FundedNext / MNQ / NQ snapshot.
// Observation only. Never submits orders. Never writes incoming / OIF.
#region Using declarations
using System;
using System.Globalization;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows;
using System.Windows.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Core;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public class AITRADEReadOnlySnapshot : AddOnBase
	{
		private DispatcherTimer timer;
		private bool started;
		private MarketData mnqMd;
		private MarketData nqMd;
		private BarsRequest mnqBars;
		private BarsRequest nq1mReq;
		private Bars nq1mLive;
		private readonly object nq1mLock = new object();
		private double? lastPx;
		private double? bidPx;
		private double? askPx;
		private DateTime? lastPxTime;
		private string lastInstr;
		private double? nqLastPx;
		private double? nqBidPx;
		private double? nqAskPx;
		private DateTime? nqLastTime;
		private double? mnqLastPx;
		private double? mnqBidPx;
		private double? mnqAskPx;
		private DateTime? mnqLastTime;
		private string barsError;
		private string connectionDump;
		private string providersJson;
		private string accountEnvironment;

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Description = "AITRADE read-only FundedNext and MNQ/NQ snapshot. Never submits orders.";
				Name = "AITRADE Read-Only Snapshot";
			}
			else if (State == State.Terminated)
			{
				StopTimer();
				Unsubscribe(ref mnqMd);
				Unsubscribe(ref nqMd);
				if (mnqBars != null)
				{
					try { mnqBars.Update -= OnBarsUpdate; } catch {}
					try { mnqBars.Dispose(); } catch {}
					mnqBars = null;
				}
				if (nq1mReq != null)
				{
					try { nq1mReq.Dispose(); } catch {}
					nq1mReq = null;
				}
				lock (nq1mLock) { nq1mLive = null; }
			}
		}

		protected override void OnWindowCreated(Window window)
		{
			StartTimer(window);
		}

		protected override void OnWindowDestroyed(Window window)
		{
		}

		private void StartTimer(Window window)
		{
			if (started)
				return;
			started = true;
			WriteSnapshot();
			Dispatcher disp = null;
			if (window != null)
				disp = window.Dispatcher;
			else if (Application.Current != null)
				disp = Application.Current.Dispatcher;
			if (disp == null)
				return;
			timer = new DispatcherTimer(DispatcherPriority.Background, disp);
			timer.Interval = TimeSpan.FromSeconds(1);
			timer.Tick += OnTick;
			timer.Start();
		}

		private void StopTimer()
		{
			if (timer != null)
			{
				timer.Stop();
				timer.Tick -= OnTick;
				timer = null;
			}
			started = false;
		}

		private void OnTick(object sender, EventArgs e)
		{
			WriteSnapshot();
		}

		private static string JsonNum(double? v)
		{
			if (v == null || double.IsNaN(v.Value) || double.IsInfinity(v.Value))
				return "null";
			return v.Value.ToString("G17", CultureInfo.InvariantCulture);
		}

		private static string JsonStr(string s)
		{
			if (s == null)
				return "null";
			return "\"" + s.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
		}

		private static string JsonBool(bool v)
		{
			return v ? "true" : "false";
		}

		private static bool IsSim(string name)
		{
			if (string.IsNullOrEmpty(name))
				return true;
			if (name.StartsWith("Sim", StringComparison.OrdinalIgnoreCase))
				return true;
			if (name.Equals("Playback101", StringComparison.OrdinalIgnoreCase) || name.Equals("Backtest", StringComparison.OrdinalIgnoreCase))
				return true;
			return false;
		}

		private static bool IsFundedNext(string name)
		{
			if (IsSim(name))
				return false;
			return name.StartsWith("FN", StringComparison.OrdinalIgnoreCase) || name.StartsWith("FUNDEDNEXT", StringComparison.OrdinalIgnoreCase);
		}

		private static bool IsTerminalOrderState(OrderState state)
		{
			return state == OrderState.Cancelled || state == OrderState.Filled || state == OrderState.Rejected;
		}

		private static bool IsKnownActiveOrderState(OrderState state)
		{
			return state == OrderState.Accepted || state == OrderState.Initialized || state == OrderState.PartFilled
				|| state == OrderState.CancelSubmitted || state == OrderState.ChangeSubmitted || state == OrderState.Submitted
				|| state == OrderState.TriggerPending || state == OrderState.Working || state == OrderState.CancelPending
				|| state == OrderState.ChangePending || state == OrderState.Suspended || state == OrderState.AcceptedByRisk;
		}

		private static bool IsRecognizedAITRADEOrder(Order order)
		{
			string name = order != null ? order.Name : null;
			string from = order != null ? order.FromEntrySignal : null;
			return (!string.IsNullOrEmpty(name) && name.StartsWith("AITRADE_", StringComparison.Ordinal))
				|| (!string.IsNullOrEmpty(from) && from.StartsWith("AITRADE_", StringComparison.Ordinal));
		}

		private static bool IsProtectiveOrder(Order order)
		{
			if (order == null)
				return false;
			string name = order.Name ?? "";
			return order.OrderState == OrderState.PartFilled || order.IsStopMarket || order.IsStopLimit
				|| name.IndexOf("stop", StringComparison.OrdinalIgnoreCase) >= 0
				|| name.IndexOf("target", StringComparison.OrdinalIgnoreCase) >= 0;
		}

		private static void AppendOrders(StringBuilder sb, Account account, bool connected, DateTime utcNow, string expectedContract, bool positionFlat)
		{
			var observed = new List<Order>();
			bool collectionAvailable = account != null && account.Orders != null;
			if (collectionAvailable)
			{
				lock (account.Orders)
				{
					foreach (Order order in account.Orders)
						if (order != null)
							observed.Add(order);
				}
			}
			var active = new List<Order>();
			var ocoCounts = new Dictionary<string, int>(StringComparer.Ordinal);
			int pending = 0, partial = 0, unknown = 0, orphan = 0;
			for (int i = 0; i < observed.Count; i++)
			{
				Order order = observed[i];
				if (IsTerminalOrderState(order.OrderState))
					continue;
				active.Add(order);
				if (!IsKnownActiveOrderState(order.OrderState))
					unknown++;
				if (order.OrderState == OrderState.PartFilled && order.Filled < order.Quantity)
					partial++;
				if (order.OrderState != OrderState.Working && order.OrderState != OrderState.PartFilled)
					pending++;
				if (!string.IsNullOrEmpty(order.Oco))
					ocoCounts[order.Oco] = ocoCounts.ContainsKey(order.Oco) ? ocoCounts[order.Oco] + 1 : 1;
			}
			for (int i = 0; i < active.Count; i++)
			{
				Order order = active[i];
				string acct = order.Account != null ? order.Account.Name : null;
				string instr = order.Instrument != null ? order.Instrument.FullName : null;
				bool ocoMissing = !string.IsNullOrEmpty(order.Oco) && (!ocoCounts.ContainsKey(order.Oco) || ocoCounts[order.Oco] < 2);
				bool potential = !IsRecognizedAITRADEOrder(order) || (positionFlat && IsProtectiveOrder(order))
					|| (positionFlat && !string.IsNullOrEmpty(order.Oco)) || ocoMissing
					|| acct != (account != null ? account.Name : null) || instr != expectedContract
					|| (order.OrderState == OrderState.PartFilled && order.Filled < order.Quantity);
				if (potential)
					orphan++;
			}
			sb.Append("\"orders\":{");
			sb.Append("\"timestamp\":").Append(JsonStr(utcNow.ToString("o"))).Append(",");
			sb.Append("\"source_heartbeat\":").Append(JsonStr(utcNow.ToString("o"))).Append(",");
			sb.Append("\"source\":\"NINJATRADER_ACCOUNT_ORDERS\",");
			sb.Append("\"connection_status\":").Append(JsonStr(connected ? "CONNECTED" : "DISCONNECTED")).Append(",");
			sb.Append("\"account_id\":").Append(JsonStr(account != null ? account.Name : null)).Append(",");
			sb.Append("\"available\":").Append(JsonBool(account != null && connected && collectionAvailable)).Append(",");
			sb.Append("\"collection_available\":").Append(JsonBool(collectionAvailable)).Append(",");
			sb.Append("\"fresh\":").Append(JsonBool(account != null && connected && collectionAvailable)).Append(",");
			sb.Append("\"total_observed\":").Append(observed.Count).Append(",");
			sb.Append("\"active_count\":").Append(active.Count).Append(",");
			sb.Append("\"pending_count\":").Append(pending).Append(",");
			sb.Append("\"partial_active_count\":").Append(partial).Append(",");
			sb.Append("\"orphan_candidate_count\":").Append(orphan).Append(",");
			sb.Append("\"unknown_count\":").Append(unknown).Append(",");
			sb.Append("\"active_orders\":[");
			for (int i = 0; i < active.Count; i++)
			{
				Order order = active[i];
				if (i > 0) sb.Append(",");
				string acct = order.Account != null ? order.Account.Name : null;
				string instr = order.Instrument != null ? order.Instrument.FullName : null;
				int remaining = Math.Max(0, order.Quantity - order.Filled);
				bool recognized = IsRecognizedAITRADEOrder(order);
				bool protective = IsProtectiveOrder(order);
				bool ocoMissing = !string.IsNullOrEmpty(order.Oco) && (!ocoCounts.ContainsKey(order.Oco) || ocoCounts[order.Oco] < 2);
				bool potential = !recognized || (positionFlat && protective) || (positionFlat && !string.IsNullOrEmpty(order.Oco))
					|| ocoMissing || acct != (account != null ? account.Name : null) || instr != expectedContract
					|| (order.OrderState == OrderState.PartFilled && remaining > 0);
				sb.Append("{");
				sb.Append("\"correlation_id\":").Append(JsonStr("NT-" + order.Id.ToString(CultureInfo.InvariantCulture))).Append(",");
				sb.Append("\"account_id\":").Append(JsonStr(acct)).Append(",");
				sb.Append("\"instrument\":").Append(JsonStr(instr)).Append(",");
				sb.Append("\"contract_month\":").Append(JsonStr(instr)).Append(",");
				sb.Append("\"action\":").Append(JsonStr(order.OrderAction.ToString())).Append(",");
				sb.Append("\"order_type\":").Append(JsonStr(order.OrderType.ToString())).Append(",");
				sb.Append("\"quantity\":").Append(order.Quantity).Append(",");
				sb.Append("\"filled_quantity\":").Append(order.Filled).Append(",");
				sb.Append("\"remaining_quantity\":").Append(remaining).Append(",");
				sb.Append("\"limit_price\":").Append(JsonNum(order.IsLimit || order.IsStopLimit ? (double?)order.LimitPrice : null)).Append(",");
				sb.Append("\"stop_price\":").Append(JsonNum(order.IsStopMarket || order.IsStopLimit ? (double?)order.StopPrice : null)).Append(",");
				sb.Append("\"state\":").Append(JsonStr(order.OrderState.ToString())).Append(",");
				sb.Append("\"oco_id\":").Append(JsonStr(string.IsNullOrEmpty(order.Oco) ? null : order.Oco)).Append(",");
				sb.Append("\"parent_correlation\":").Append(JsonStr(string.IsNullOrEmpty(order.FromEntrySignal) ? null : order.FromEntrySignal)).Append(",");
				sb.Append("\"created_at\":").Append(JsonStr(order.Time.ToUniversalTime().ToString("o"))).Append(",");
				sb.Append("\"updated_at\":").Append(JsonStr(utcNow.ToString("o"))).Append(",");
				sb.Append("\"recognized\":").Append(JsonBool(recognized)).Append(",");
				sb.Append("\"protective\":").Append(JsonBool(protective)).Append(",");
				sb.Append("\"potential_orphan\":").Append(JsonBool(potential));
				sb.Append("}");
			}
			sb.Append("]},");
		}

		private static DateTime ThirdFriday(int year, int month)
		{
			DateTime d = new DateTime(year, month, 1);
			int offset = ((int)DayOfWeek.Friday - (int)d.DayOfWeek + 7) % 7;
			return d.AddDays(offset + 14);
		}

		private static string FrontMonthCode(DateTime utcNow)
		{
			DateTime day = utcNow.Date;
			int[] months = { 3, 6, 9, 12 };
			for (int y = day.Year; y <= day.Year + 1; y++)
			{
				for (int i = 0; i < months.Length; i++)
				{
					int m = months[i];
					DateTime expiry = ThirdFriday(y, m);
					if (expiry.Date >= day)
						return string.Format("{0:00}-{1:00}", m, y % 100);
				}
			}
			return string.Format("{0:00}-{1:00}", 12, day.Year % 100);
		}

		private static DateTime FrontMonthExpiry(DateTime utcNow)
		{
			DateTime day = utcNow.Date;
			int[] months = { 3, 6, 9, 12 };
			for (int y = day.Year; y <= day.Year + 1; y++)
			{
				for (int i = 0; i < months.Length; i++)
				{
					int m = months[i];
					DateTime expiry = ThirdFriday(y, m);
					if (expiry.Date >= day)
						return expiry;
				}
			}
			return ThirdFriday(day.Year, 12);
		}

		private static Instrument FindContract(string rootName, string code)
		{
			string[] names = {
				rootName + " " + code,
				rootName + " " + code + " Globex"
			};
			for (int i = 0; i < names.Length; i++)
			{
				try
				{
					Instrument inst = Instrument.GetInstrument(names[i]);
					if (inst != null)
						return inst;
				}
				catch
				{
				}
			}
			Instrument fallback = null;
			string needle = code.Replace("-", "");
			foreach (Instrument inst in Instrument.All)
			{
				string full = inst.FullName ?? "";
				string master = inst.MasterInstrument != null ? inst.MasterInstrument.Name : "";
				if (!master.Equals(rootName, StringComparison.OrdinalIgnoreCase) && !full.StartsWith(rootName, StringComparison.OrdinalIgnoreCase))
					continue;
				fallback = inst;
				if (full.IndexOf(code, StringComparison.OrdinalIgnoreCase) >= 0 || full.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0)
					return inst;
			}
			return fallback;
		}

		private static void Stamp(DateTime t, ref DateTime? dest)
		{
			if (t.Year > 2000)
				dest = t.Kind == DateTimeKind.Utc ? t : t.ToUniversalTime();
		}

		private void ApplyTypedQuote(string fullName, MarketDataType mdType, double price, DateTime time)
		{
			bool isNq = !string.IsNullOrEmpty(fullName) && fullName.StartsWith("NQ", StringComparison.OrdinalIgnoreCase)
				&& !fullName.StartsWith("MNQ", StringComparison.OrdinalIgnoreCase);
			bool isMnq = !string.IsNullOrEmpty(fullName) && fullName.StartsWith("MNQ", StringComparison.OrdinalIgnoreCase);
			if (mdType == MarketDataType.Last)
			{
				lastPx = price;
				Stamp(time, ref lastPxTime);
				if (isNq) { nqLastPx = price; Stamp(time, ref nqLastTime); }
				if (isMnq) { mnqLastPx = price; Stamp(time, ref mnqLastTime); }
			}
			else if (mdType == MarketDataType.Bid)
			{
				bidPx = price;
				if (lastPxTime == null)
					Stamp(time, ref lastPxTime);
				if (isNq) nqBidPx = price;
				if (isMnq) mnqBidPx = price;
			}
			else if (mdType == MarketDataType.Ask)
			{
				askPx = price;
				if (isNq) nqAskPx = price;
				if (isMnq) mnqAskPx = price;
			}
		}

		private void OnMarketData(object sender, MarketDataEventArgs e)
		{
			if (e == null)
				return;
			try
			{
				if (e.Instrument != null)
					lastInstr = e.Instrument.FullName;
				ApplyTypedQuote(lastInstr, e.MarketDataType, e.Price, e.Time);
			}
			catch
			{
			}
		}

		private void Unsubscribe(ref MarketData md)
		{
			if (md == null)
				return;
			try { md.Update -= OnMarketData; } catch {}
			md = null;
		}

		private MarketData EnsureMarketData(Instrument inst, MarketData existing)
		{
			if (inst == null)
				return existing;
			if (existing != null)
				return existing;
			try
			{
				MarketData md = new MarketData(inst);
				md.Update += OnMarketData;
				return md;
			}
			catch
			{
				return existing;
			}
		}

		private static double? NonZero(double? v)
		{
			if (v == null || Math.Abs(v.Value) < 1e-12)
				return null;
			return v;
		}

		private static double? TryGet(Account acct, AccountItem item)
		{
			if (acct == null)
				return null;
			try
			{
				double v = acct.Get(item, Currency.UsDollar);
				if (double.IsNaN(v) || double.IsInfinity(v))
					return null;
				return v;
			}
			catch
			{
				return null;
			}
		}

		private static void AppendItem(StringBuilder sb, string name, double? v, ref bool first)
		{
			if (!first)
				sb.Append(",");
			first = false;
			sb.Append(JsonStr(name)).Append(":").Append(JsonNum(v));
		}

		private static string OptionText(object opts, params string[] names)
		{
			if (opts == null)
				return "";
			try
			{
				Type t = opts.GetType();
				for (int i = 0; i < names.Length; i++)
				{
					var p = t.GetProperty(names[i]);
					if (p == null)
						continue;
					object v = p.GetValue(opts, null);
					if (v != null)
						return v.ToString();
				}
			}
			catch
			{
			}
			return "";
		}

		private static string ClassifyProviderKind(string name, string typeName)
		{
			string n = name ?? "";
			string t = typeName ?? "";
			string nl = n.ToLowerInvariant();
			string tl = t.ToLowerInvariant();
			string blob = (n + " " + t).ToLowerInvariant();
			// Artificial simulator backend. Display name "Simulation" is not sufficient.
			if (nl.IndexOf("simulated data feed", StringComparison.Ordinal) >= 0
				|| tl.IndexOf("simulatoroptions", StringComparison.Ordinal) >= 0
				|| tl.Equals("simulator"))
				return "ARTIFICIAL";
			if (blob.IndexOf("playback", StringComparison.OrdinalIgnoreCase) >= 0)
				return "PLAYBACK";
			if (blob.IndexOf("end of day", StringComparison.OrdinalIgnoreCase) >= 0
				|| blob.IndexOf(" eod", StringComparison.OrdinalIgnoreCase) >= 0
				|| n.IndexOf("Delayed", StringComparison.OrdinalIgnoreCase) >= 0)
				return "DELAYED";
			if (tl.IndexOf("tradovate", StringComparison.Ordinal) >= 0
				|| tl.IndexOf("continuum", StringComparison.Ordinal) >= 0
				|| nl.Equals("ninjatrader")
				|| (nl.IndexOf("ninjatrader", StringComparison.Ordinal) >= 0 && nl.IndexOf("simulated", StringComparison.Ordinal) < 0))
				return "TRADOVATE";
			if (n.Equals("Simulation", StringComparison.OrdinalIgnoreCase)
				|| n.Equals("Simulation.txt", StringComparison.OrdinalIgnoreCase))
				return "ARTIFICIAL";
			if (blob.IndexOf("cqg", StringComparison.OrdinalIgnoreCase) >= 0)
				return "CQG";
			if (blob.IndexOf("rithmic", StringComparison.OrdinalIgnoreCase) >= 0)
				return "RITHMIC";
			if (blob.IndexOf("kinetick", StringComparison.OrdinalIgnoreCase) >= 0)
				return "KINETICK";
			return "OTHER";
		}

		private void ClassifyFeeds(out bool marketConnected, out string quality, out string marketStatus, out string dump)
		{
			bool liveConn = false;
			bool simConn = false;
			bool delayedConn = false;
			bool playbackConn = false;
			bool anyConnected = false;
			string env = null;
			var dumpSb = new StringBuilder();
			var provSb = new StringBuilder();
			provSb.Append("[");
			bool firstProv = true;
			try
			{
				lock (Connection.Connections)
				{
					foreach (Connection c in Connection.Connections)
					{
						if (c == null)
							continue;
						string n = "";
						string st = "";
						string typeName = "";
						try
						{
							if (c.Options != null && c.Options.Name != null)
								n = c.Options.Name;
							else
								n = c.ToString();
						}
						catch
						{
							try { n = c.ToString(); } catch { n = "connection"; }
						}
						try { st = c.Status.ToString(); } catch { st = "Unknown"; }
						try { if (c.Options != null) typeName = c.Options.GetType().Name; } catch { typeName = ""; }
						string providerEnum = "";
						int? providerId = null;
						try
						{
							if (c.Options != null)
							{
								providerEnum = c.Options.Provider.ToString();
								providerId = Convert.ToInt32(c.Options.Provider);
							}
						}
						catch
						{
						}
						string acctType = OptionText(c.Options, "AccountType", "Mode", "TradingMode", "TradovateAccountType");
						string kind = ClassifyProviderKind(n, typeName);
						if (dumpSb.Length > 0)
							dumpSb.Append(";");
						dumpSb.Append(n).Append("=").Append(st);
						bool connected = false;
						try { connected = c.Status == ConnectionStatus.Connected; } catch {}
						if (!firstProv)
							provSb.Append(",");
						firstProv = false;
						provSb.Append("{");
						provSb.Append("\"name\":").Append(JsonStr(n)).Append(",");
						provSb.Append("\"provider_display_name\":").Append(JsonStr(n)).Append(",");
						provSb.Append("\"status\":").Append(JsonStr(st)).Append(",");
						provSb.Append("\"provider_type\":").Append(JsonStr(typeName)).Append(",");
						provSb.Append("\"provider_backend\":").Append(JsonStr(typeName)).Append(",");
						provSb.Append("\"provider_kind\":").Append(JsonStr(kind)).Append(",");
						provSb.Append("\"provider_enum\":").Append(JsonStr(providerEnum)).Append(",");
						provSb.Append("\"provider_id\":").Append(providerId.HasValue ? providerId.Value.ToString() : "null").Append(",");
						provSb.Append("\"account_environment\":").Append(JsonStr(string.IsNullOrEmpty(acctType) ? null : acctType.ToUpperInvariant())).Append(",");
						provSb.Append("\"connected\":").Append(JsonBool(connected));
						provSb.Append("}");
						if (connected && kind == "TRADOVATE" && !string.IsNullOrEmpty(acctType))
							env = acctType.ToUpperInvariant();
						if (!connected)
							continue;
						anyConnected = true;
						if (kind == "ARTIFICIAL")
							simConn = true;
						else if (kind == "PLAYBACK")
							playbackConn = true;
						else if (kind == "DELAYED")
							delayedConn = true;
						else
							liveConn = true;
					}
				}
			}
			catch (Exception ex)
			{
				dumpSb.Append("err=").Append(ex.GetType().Name);
			}
			provSb.Append("]");
			providersJson = provSb.ToString();
			if (string.IsNullOrEmpty(env) && liveConn)
				env = "SIMULATION";
			accountEnvironment = env;
			dump = dumpSb.ToString();
			if (!anyConnected)
			{
				marketConnected = false;
				quality = "UNKNOWN";
				marketStatus = "DISCONNECTED";
				return;
			}
			marketConnected = anyConnected;
			if (liveConn)
				quality = "UNKNOWN";
			else if (delayedConn)
				quality = "DELAYED";
			else if (playbackConn)
				quality = "PLAYBACK";
			else if (simConn)
				quality = "SIMULATED";
			else
				quality = "UNKNOWN";
			marketStatus = "CONNECTED";
		}

		private void OnBarsUpdate(object sender, BarsUpdateEventArgs e)
		{
			if (e == null || e.BarsSeries == null || e.MaxIndex < 0)
				return;
			try
			{
				int i = e.MaxIndex;
				lastPx = e.BarsSeries.GetClose(i);
				DateTime t = e.BarsSeries.GetTime(i);
				if (t.Year > 2000)
					lastPxTime = t.Kind == DateTimeKind.Utc ? t : t.ToUniversalTime();
				try { bidPx = e.BarsSeries.GetBid(i); } catch {}
				try { askPx = e.BarsSeries.GetAsk(i); } catch {}
				if (e.BarsSeries.Instrument != null)
					lastInstr = e.BarsSeries.Instrument.FullName;
			}
			catch
			{
			}
		}

		private void ApplyBars(Bars bars)
		{
			if (bars == null || bars.Count <= 0)
				return;
			try
			{
				int i = bars.Count - 1;
				lastPx = bars.GetClose(i);
				DateTime t = bars.GetTime(i);
				if (t.Year > 2000)
					lastPxTime = t.Kind == DateTimeKind.Utc ? t : t.ToUniversalTime();
				try { bidPx = bars.GetBid(i); } catch {}
				try { askPx = bars.GetAsk(i); } catch {}
				if (bars.Instrument != null)
					lastInstr = bars.Instrument.FullName;
			}
			catch
			{
			}
		}

		private void EnsureBars(Instrument inst)
		{
			if (inst == null || mnqBars != null)
				return;
			try
			{
				mnqBars = new BarsRequest(inst, 32);
				mnqBars.BarsPeriod = new BarsPeriod { BarsPeriodType = BarsPeriodType.Tick, Value = 1 };
				try { mnqBars.TradingHours = TradingHours.Get("Default 24 x 7"); } catch {}
				mnqBars.Update += OnBarsUpdate;
				mnqBars.Request((bars, errorCode, errorMessage) =>
				{
					if (errorCode != ErrorCode.NoError)
					{
						barsError = errorCode.ToString() + " " + (errorMessage ?? "");
						return;
					}
					if (bars != null)
						ApplyBars(bars.Bars);
				});
			}
			catch (Exception ex)
			{
				barsError = ex.GetType().Name + ": " + ex.Message;
			}
		}

		private void EnsureNqMinuteBars(Instrument inst)
		{
			if (inst == null || nq1mReq != null)
				return;
			try
			{
				DateTime from = DateTime.Now.AddHours(-18);
				nq1mReq = new BarsRequest(inst, from, DateTime.Now.AddMinutes(2));
				nq1mReq.BarsPeriod = new BarsPeriod { BarsPeriodType = BarsPeriodType.Minute, Value = 1 };
				try { nq1mReq.TradingHours = TradingHours.Get("CME US Index Futures ETH"); } catch
				{
					try { nq1mReq.TradingHours = TradingHours.Get("Default 24 x 7"); } catch {}
				}
				nq1mReq.Update += OnNq1mUpdate;
				nq1mReq.Request((bars, errorCode, errorMessage) =>
				{
					if (errorCode != ErrorCode.NoError)
					{
						barsError = errorCode.ToString() + " " + (errorMessage ?? "");
						return;
					}
					if (bars != null && bars.Bars != null)
					{
						lock (nq1mLock)
							nq1mLive = bars.Bars;
					}
				});
			}
			catch (Exception ex)
			{
				barsError = ex.GetType().Name + ": " + ex.Message;
			}
		}

		private void OnNq1mUpdate(object sender, BarsUpdateEventArgs e)
		{
			try
			{
				if (nq1mReq != null && nq1mReq.Bars != null)
				{
					lock (nq1mLock)
						nq1mLive = nq1mReq.Bars;
				}
			}
			catch
			{
			}
		}

		private static void ReadMnqPosition(Account acct, Instrument mnq, string fallbackName, out string side, out int qty, out double? avg, out string instr)
		{
			side = "FLAT";
			qty = 0;
			avg = null;
			instr = fallbackName;
			if (acct == null)
				return;
			lock (acct.Positions)
			{
				foreach (Position p in acct.Positions)
				{
					if (p.Instrument == null)
						continue;
					string full = p.Instrument.FullName ?? "";
					bool isMnq = full.StartsWith("MNQ", StringComparison.OrdinalIgnoreCase)
						|| (mnq != null && p.Instrument == mnq);
					if (!isMnq)
						continue;
					instr = full;
					qty = p.Quantity;
					string mp = p.MarketPosition.ToString();
					if (qty > 0 && mp.Equals("Long", StringComparison.OrdinalIgnoreCase))
						side = "LONG";
					else if (qty > 0 && mp.Equals("Short", StringComparison.OrdinalIgnoreCase))
						side = "SHORT";
					else
					{
						side = "FLAT";
						qty = 0;
					}
					if (side != "FLAT")
						avg = p.AveragePrice;
					return;
				}
			}
		}

		private void AppendCompletedNq1m(StringBuilder sb, DateTime utcNow)
		{
			sb.Append("\"nq_bars_1m\":[");
			Bars bars = null;
			lock (nq1mLock)
				bars = nq1mLive;
			int n = 0;
			if (bars != null && bars.Count > 1)
			{
				TimeZoneInfo et;
				try { et = TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time"); }
				catch { et = TimeZoneInfo.FindSystemTimeZoneById("America/New_York"); }
				DateTime epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
				int start = Math.Max(0, bars.Count - 500);
				for (int i = start; i < bars.Count; i++)
				{
					DateTime t = bars.GetTime(i);
					DateTime etTime;
					if (t.Kind == DateTimeKind.Utc)
						etTime = TimeZoneInfo.ConvertTimeFromUtc(t, et);
					else
						etTime = DateTime.SpecifyKind(t, DateTimeKind.Unspecified);
					DateTime utcEnd;
					try { utcEnd = TimeZoneInfo.ConvertTimeToUtc(etTime.AddMinutes(1), et); }
					catch { continue; }
					if (utcEnd > utcNow)
						continue;
					DateTime utcOpen;
					try { utcOpen = TimeZoneInfo.ConvertTimeToUtc(etTime, et); }
					catch { continue; }
					long unix = (long)(utcOpen - epoch).TotalSeconds;
					double vol = 0;
					try { vol = bars.GetVolume(i); } catch {}
					if (n > 0)
						sb.Append(",");
					sb.Append("{");
					sb.Append("\"time\":").Append(unix.ToString(CultureInfo.InvariantCulture)).Append(",");
					sb.Append("\"open\":").Append(JsonNum(bars.GetOpen(i))).Append(",");
					sb.Append("\"high\":").Append(JsonNum(bars.GetHigh(i))).Append(",");
					sb.Append("\"low\":").Append(JsonNum(bars.GetLow(i))).Append(",");
					sb.Append("\"close\":").Append(JsonNum(bars.GetClose(i))).Append(",");
					sb.Append("\"volume\":").Append(JsonNum(vol)).Append(",");
					sb.Append("\"iso_et\":").Append(JsonStr(etTime.ToString("yyyy-MM-ddTHH:mm:ss"))).Append(",");
					sb.Append("\"finalized\":true");
					sb.Append("}");
					n++;
				}
			}
			sb.Append("],");
			sb.Append("\"nq_bars_1m_count\":").Append(n).Append(",");
			sb.Append("\"nq_bars_1m_status\":").Append(JsonStr(n > 0 ? "LIVE" : (nq1mReq != null ? "WAITING" : "UNAVAILABLE"))).Append(",");
		}

		private void WriteSnapshot()
		{
			try
			{
				Account fn = null;
				Account sim = null;
				foreach (Account a in Account.All)
				{
					if (fn == null && IsFundedNext(a.Name))
						fn = a;
					if (sim == null && a.Name == "Sim101")
						sim = a;
				}

				DateTime utcNow = DateTime.UtcNow;
				string code = FrontMonthCode(utcNow);
				DateTime expiry = FrontMonthExpiry(utcNow);
				Instrument mnq = FindContract("MNQ", code);
				Instrument nq = FindContract("NQ", code);
				mnqMd = EnsureMarketData(mnq, mnqMd);
				nqMd = EnsureMarketData(nq, nqMd);
				EnsureBars(mnq);
				EnsureNqMinuteBars(nq);

				double? cash = NonZero(TryGet(fn, AccountItem.CashValue));
				double? net = NonZero(TryGet(fn, AccountItem.NetLiquidation));
				double? unreal = TryGet(fn, AccountItem.UnrealizedProfitLoss);
				double? realized = TryGet(fn, AccountItem.RealizedProfitLoss);
				double? buying = NonZero(TryGet(fn, AccountItem.BuyingPower));
				double? initMarg = NonZero(TryGet(fn, AccountItem.InitialMargin));
				double? maintMarg = NonZero(TryGet(fn, AccountItem.MaintenanceMargin));
				if (cash == null && net == null)
				{
					unreal = null;
					realized = null;
				}

				bool fnConnected = false;
				string acctStatus = "Unknown";
				string acctName = fn != null ? fn.Name : null;
				string acctConnName = null;
				string acctConnType = null;
				if (fn != null && fn.Connection != null)
				{
					acctStatus = fn.Connection.Status.ToString();
					fnConnected = fn.Connection.Status == ConnectionStatus.Connected;
					try
					{
						if (fn.Connection.Options != null)
						{
							acctConnName = fn.Connection.Options.Name;
							acctConnType = fn.Connection.Options.GetType().Name;
						}
					}
					catch
					{
					}
				}

				string posInstr = mnq != null ? mnq.FullName : ("MNQ " + code);
				string posSide = "FLAT";
				int posQty = 0;
				double? posAvg = null;
				if (fn != null)
					ReadMnqPosition(fn, mnq, posInstr, out posSide, out posQty, out posAvg, out posInstr);

				string simInstr = mnq != null ? mnq.FullName : ("MNQ " + code);
				string simSide = "FLAT";
				int simQty = 0;
				double? simAvg = null;
				bool simPresent = sim != null;
				if (simPresent)
					ReadMnqPosition(sim, mnq, simInstr, out simSide, out simQty, out simAvg, out simInstr);

				double? last = lastPx;
				double? bid = bidPx;
				double? ask = askPx;
				DateTime? lastDt = lastPxTime;
				string mktInstr = lastInstr ?? (mnq != null ? mnq.FullName : ("MNQ " + code));
				string mnqName = mnq != null ? mnq.FullName : ("MNQ " + code);
				string nqName = nq != null ? nq.FullName : ("NQ " + code);
				ApplyQuote(mnq, ref mnqLastPx, ref mnqBidPx, ref mnqAskPx, ref mnqLastTime, ref mnqName);
				ApplyQuote(nq, ref nqLastPx, ref nqBidPx, ref nqAskPx, ref nqLastTime, ref nqName);
				if (mnqMd != null)
					ApplyMd(mnqMd, ref mnqLastPx, ref mnqBidPx, ref mnqAskPx, ref mnqLastTime);
				if (nqMd != null)
					ApplyMd(nqMd, ref nqLastPx, ref nqBidPx, ref nqAskPx, ref nqLastTime);
				if (last == null) last = mnqLastPx ?? nqLastPx;
				if (bid == null) bid = mnqBidPx ?? nqBidPx;
				if (ask == null) ask = mnqAskPx ?? nqAskPx;
				if (lastDt == null) lastDt = mnqLastTime ?? nqLastTime;
				if (mnqLastPx != null)
					mktInstr = mnqName;

				string lastTime = null;
				if (lastDt != null && lastDt.Value.Year > 2000)
					lastTime = lastDt.Value.ToString("o");
				string nqTime = (nqLastTime != null && nqLastTime.Value.Year > 2000) ? nqLastTime.Value.ToString("o") : null;
				string mnqTime = (mnqLastTime != null && mnqLastTime.Value.Year > 2000) ? mnqLastTime.Value.ToString("o") : null;

				double? age = null;
				if (lastDt != null)
					age = Math.Max(0.0, (utcNow - lastDt.Value).TotalSeconds);

				bool marketConnected;
				string quality;
				string marketStatus;
				string connDump;
				ClassifyFeeds(out marketConnected, out quality, out marketStatus, out connDump);
				connectionDump = connDump;
				if (last != null)
				{
					marketConnected = true;
					if (marketStatus == "DISCONNECTED")
						marketStatus = "CONNECTED";
				}
				bool fresh = last != null && bid != null && ask != null && lastTime != null && age != null && age.Value <= 120.0
					&& quality != "SIMULATED" && quality != "PLAYBACK" && quality != "DELAYED";

				bool valuesPresent = cash != null || net != null;
				string valueSource = valuesPresent ? "NINJATRADER_RUNTIME" : "UNAVAILABLE_FROM_NINJATRADER_RUNTIME";

				var sb = new StringBuilder();
				sb.Append("{");
				sb.Append("\"schema\":\"AITRADE_NT_READONLY_V1\",");
				sb.Append("\"source\":\"NINJATRADER_READ_ONLY\",");
				sb.Append("\"PROP_EXECUTION\":false,");
				sb.Append("\"orders_transmitted\":0,");
				sb.Append("\"read_only\":true,");
				sb.Append("\"timestamp\":").Append(JsonStr(utcNow.ToString("o"))).Append(",");
				sb.Append("\"ts\":").Append(JsonStr(utcNow.ToString("o"))).Append(",");
				sb.Append("\"market_data_quality\":").Append(JsonStr(quality)).Append(",");
				sb.Append("\"account_environment\":").Append(JsonStr(accountEnvironment)).Append(",");
				sb.Append("\"connection\":{");
				sb.Append("\"status\":").Append(JsonStr(fnConnected ? "CONNECTED" : acctStatus.ToUpperInvariant())).Append(",");
				sb.Append("\"account\":").Append(JsonStr(fnConnected ? "CONNECTED" : acctStatus.ToUpperInvariant())).Append(",");
				sb.Append("\"market\":").Append(JsonStr(marketStatus)).Append(",");
				sb.Append("\"quality\":").Append(JsonStr(quality)).Append(",");
				sb.Append("\"feed\":").Append(JsonStr(quality)).Append(",");
				sb.Append("\"account_environment\":").Append(JsonStr(accountEnvironment));
				sb.Append("},");
				sb.Append("\"contracts\":{");
				sb.Append("\"nq\":").Append(JsonStr("NQ " + code)).Append(",");
				sb.Append("\"mnq\":").Append(JsonStr("MNQ " + code)).Append(",");
				sb.Append("\"expiry\":").Append(JsonStr(expiry.ToString("yyyy-MM-dd"))).Append(",");
				sb.Append("\"code\":").Append(JsonStr(code)).Append(",");
				sb.Append("\"signal_instrument\":\"NQ\",");
				sb.Append("\"position_instrument\":\"MNQ\",");
				sb.Append("\"mapping\":\"NQ_DRIFT_VWAP_PULLBACK signal on NQ -> MNQ evaluation size\"");
				sb.Append("},");
				sb.Append("\"nq\":{");
				sb.Append("\"instrument\":").Append(JsonStr(nqName)).Append(",");
				sb.Append("\"last\":").Append(JsonNum(nqLastPx)).Append(",");
				sb.Append("\"bid\":").Append(JsonNum(nqBidPx)).Append(",");
				sb.Append("\"ask\":").Append(JsonNum(nqAskPx)).Append(",");
				sb.Append("\"timestamp\":").Append(JsonStr(nqTime)).Append(",");
				sb.Append("\"last_update\":").Append(JsonStr(nqTime));
				sb.Append("},");
				AppendCompletedNq1m(sb, utcNow);
				sb.Append("\"mnq\":{");
				sb.Append("\"instrument\":").Append(JsonStr(mnqName)).Append(",");
				sb.Append("\"last\":").Append(JsonNum(mnqLastPx)).Append(",");
				sb.Append("\"bid\":").Append(JsonNum(mnqBidPx)).Append(",");
				sb.Append("\"ask\":").Append(JsonNum(mnqAskPx)).Append(",");
				sb.Append("\"timestamp\":").Append(JsonStr(mnqTime)).Append(",");
				sb.Append("\"last_update\":").Append(JsonStr(mnqTime));
				sb.Append("},");
				sb.Append("\"market_data\":{");
				sb.Append("\"source\":\"NINJATRADER_READ_ONLY\",");
				sb.Append("\"instrument\":").Append(JsonStr(mktInstr)).Append(",");
				sb.Append("\"last\":").Append(JsonNum(last)).Append(",");
				sb.Append("\"bid\":").Append(JsonNum(bid)).Append(",");
				sb.Append("\"ask\":").Append(JsonNum(ask)).Append(",");
				sb.Append("\"last_update\":").Append(JsonStr(lastTime)).Append(",");
				sb.Append("\"timestamp\":").Append(JsonStr(lastTime)).Append(",");
				sb.Append("\"age_sec\":").Append(JsonNum(age)).Append(",");
				sb.Append("\"quality\":").Append(JsonStr(quality)).Append(",");
				sb.Append("\"connected\":").Append(JsonBool(marketConnected)).Append(",");
				sb.Append("\"fresh\":").Append(JsonBool(fresh));
				sb.Append("},");
				sb.Append("\"market\":{");
				sb.Append("\"instrument\":").Append(JsonStr(mktInstr)).Append(",");
				sb.Append("\"last\":").Append(JsonNum(last)).Append(",");
				sb.Append("\"bid\":").Append(JsonNum(bid)).Append(",");
				sb.Append("\"ask\":").Append(JsonNum(ask)).Append(",");
				sb.Append("\"last_time\":").Append(JsonStr(lastTime)).Append(",");
				sb.Append("\"last_update\":").Append(JsonStr(lastTime)).Append(",");
				sb.Append("\"age_sec\":").Append(JsonNum(age)).Append(",");
				sb.Append("\"quality\":").Append(JsonStr(quality));
				sb.Append("},");
				sb.Append("\"fundednext\":{");
				sb.Append("\"account_id\":").Append(JsonStr(acctName)).Append(",");
				sb.Append("\"connected\":").Append(JsonBool(fnConnected)).Append(",");
				sb.Append("\"connection_name\":").Append(JsonStr(acctConnName)).Append(",");
				sb.Append("\"connection_backend\":").Append(JsonStr(acctConnType)).Append(",");
				sb.Append("\"read_only\":true,");
				sb.Append("\"cash_value\":").Append(JsonNum(cash)).Append(",");
				sb.Append("\"net_liquidation\":").Append(JsonNum(net)).Append(",");
				sb.Append("\"realized_pnl\":").Append(JsonNum(realized)).Append(",");
				sb.Append("\"unrealized_pnl\":").Append(JsonNum(unreal)).Append(",");
				sb.Append("\"buying_power\":").Append(JsonNum(buying)).Append(",");
				sb.Append("\"initial_margin\":").Append(JsonNum(initMarg)).Append(",");
				sb.Append("\"maintenance_margin\":").Append(JsonNum(maintMarg)).Append(",");
				sb.Append("\"value_source\":").Append(JsonStr(valueSource));
				sb.Append("},");
				sb.Append("\"account\":{");
				sb.Append("\"id\":").Append(JsonStr(acctName)).Append(",");
				sb.Append("\"kind\":\"FUNDEDNEXT\",");
				sb.Append("\"connection\":").Append(JsonStr(acctStatus)).Append(",");
				sb.Append("\"cash_value\":").Append(JsonNum(cash)).Append(",");
				sb.Append("\"net_liquidation\":").Append(JsonNum(net)).Append(",");
				sb.Append("\"unrealized\":").Append(JsonNum(unreal)).Append(",");
				sb.Append("\"realized\":").Append(JsonNum(realized));
				sb.Append("},");
				sb.Append("\"available_account_items\":{");
				bool firstItem = true;
				AppendItem(sb, "CashValue", cash, ref firstItem);
				AppendItem(sb, "NetLiquidation", net, ref firstItem);
				AppendItem(sb, "RealizedProfitLoss", realized, ref firstItem);
				AppendItem(sb, "UnrealizedProfitLoss", unreal, ref firstItem);
				AppendItem(sb, "BuyingPower", buying, ref firstItem);
				AppendItem(sb, "InitialMargin", initMarg, ref firstItem);
				AppendItem(sb, "MaintenanceMargin", maintMarg, ref firstItem);
				sb.Append("},");
				sb.Append("\"sim101_excluded\":false,");
				sb.Append("\"sim101\":{");
				sb.Append("\"present\":").Append(JsonBool(simPresent)).Append(",");
				sb.Append("\"excluded\":false,");
				sb.Append("\"money_excluded\":true,");
				sb.Append("\"read_only\":true,");
				sb.Append("\"account\":").Append(JsonStr(simPresent ? "Sim101" : null)).Append(",");
				sb.Append("\"id\":").Append(JsonStr(simPresent ? sim.Name : null));
				if (simPresent)
				{
					sb.Append(",");
					sb.Append("\"position\":{");
					sb.Append("\"instrument\":").Append(JsonStr(simInstr)).Append(",");
					sb.Append("\"quantity\":").Append(simQty).Append(",");
					sb.Append("\"side\":").Append(JsonStr(simSide)).Append(",");
					sb.Append("\"average_price\":").Append(JsonNum(simAvg)).Append(",");
					sb.Append("\"timestamp\":").Append(JsonStr(utcNow.ToString("o"))).Append(",");
					sb.Append("\"source\":\"NINJATRADER_ACCOUNT_POSITION\"");
					sb.Append("}");
				}
				else
				{
					sb.Append(",\"position\":null");
				}
				sb.Append("},");
				sb.Append("\"position\":{");
				sb.Append("\"instrument\":").Append(JsonStr(posInstr)).Append(",");
				sb.Append("\"side\":").Append(JsonStr(posSide)).Append(",");
				sb.Append("\"market_position\":").Append(JsonStr(posSide)).Append(",");
				sb.Append("\"quantity\":").Append(posQty).Append(",");
				sb.Append("\"average_price\":").Append(JsonNum(posAvg));
				sb.Append("},");
				AppendOrders(sb, fn, fnConnected, utcNow, posInstr, posQty == 0 && posSide == "FLAT");
				sb.Append("\"diagnostics\":{");
				sb.Append("\"mnq_found\":").Append(JsonBool(mnq != null)).Append(",");
				sb.Append("\"nq_found\":").Append(JsonBool(nq != null)).Append(",");
				sb.Append("\"mnq_name\":").Append(JsonStr(mnq != null ? mnq.FullName : null)).Append(",");
				sb.Append("\"nq_name\":").Append(JsonStr(nq != null ? nq.FullName : null)).Append(",");
				sb.Append("\"market_data_subscribed\":").Append(JsonBool(mnqMd != null || nqMd != null)).Append(",");
				sb.Append("\"bars_request\":").Append(JsonBool(mnqBars != null)).Append(",");
				sb.Append("\"nq_1m_bars_request\":").Append(JsonBool(nq1mReq != null)).Append(",");
				sb.Append("\"bars_error\":").Append(JsonStr(barsError)).Append(",");
				sb.Append("\"connections\":").Append(JsonStr(connectionDump)).Append(",");
				sb.Append("\"providers\":").Append(string.IsNullOrEmpty(providersJson) ? "[]" : providersJson);
				sb.Append("}}");

				string dir = Path.Combine(Globals.UserDataDir, "outgoing");
				Directory.CreateDirectory(dir);
				string dest = Path.Combine(dir, "AITRADE_READONLY.json");
				string tmp = dest + ".tmp";
				File.WriteAllText(tmp, sb.ToString(), new UTF8Encoding(false));
				File.Copy(tmp, dest, true);
				File.Delete(tmp);
			}
			catch (Exception ex)
			{
				try
				{
					string dir = Path.Combine(Globals.UserDataDir, "outgoing");
					Directory.CreateDirectory(dir);
					File.AppendAllText(
						Path.Combine(dir, "AITRADE_READONLY.log"),
						DateTime.UtcNow.ToString("o") + " " + ex.GetType().Name + ": " + ex.Message + Environment.NewLine);
				}
				catch
				{
				}
			}
		}

		private static void ApplyQuote(Instrument inst, ref double? last, ref double? bid, ref double? ask, ref DateTime? lastDt, ref string mktInstr)
		{
			if (inst == null || inst.MarketData == null)
				return;
			try
			{
				if (last == null && inst.MarketData.Last != null)
				{
					last = inst.MarketData.Last.Price;
					DateTime t = inst.MarketData.Last.Time;
					if (t.Year > 2000)
						lastDt = t.Kind == DateTimeKind.Utc ? t : t.ToUniversalTime();
					mktInstr = inst.FullName;
				}
				if (bid == null && inst.MarketData.Bid != null)
					bid = inst.MarketData.Bid.Price;
				if (ask == null && inst.MarketData.Ask != null)
					ask = inst.MarketData.Ask.Price;
			}
			catch
			{
			}
		}

		private static void ApplyMd(MarketData md, ref double? last, ref double? bid, ref double? ask, ref DateTime? lastDt)
		{
			if (md == null)
				return;
			try
			{
				if (last == null && md.Last != null)
				{
					last = md.Last.Price;
					DateTime t = md.Last.Time;
					if (t.Year > 2000)
						lastDt = t.Kind == DateTimeKind.Utc ? t : t.ToUniversalTime();
				}
				if (bid == null && md.Bid != null)
					bid = md.Bid.Price;
				if (ask == null && md.Ask != null)
					ask = md.Ask.Price;
			}
			catch
			{
			}
		}
	}
}
