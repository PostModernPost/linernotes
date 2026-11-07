package app.linernotes

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.Message
import android.view.MotionEvent
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.core.content.FileProvider
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.URL
import java.util.concurrent.Executors

/**
 * The phone app: finds Liner Notes on the PC (over USB, or on the Wi-Fi), connects, and shows the same
 * screen the PC shows, full screen. The microphone, keeping the screen on, waking it and sharing are
 * handled here (the page calls window.LinerNotesApp), so they also work over Wi-Fi.
 */
class MainActivity : Activity() {

    private lateinit var web: WebView
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private var server: String? = null           // the PC page currently shown
    private var finder: Finder? = null
    private val found = linkedMapOf<String, String>()   // url -> PC name
    private var pairing: Pairing.Start? = null
    private var pairUrl: String? = null
    private var dark = false
    private var askedMic = false
    private var lastTouch = 0L                   // when the screen was last touched

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        lastTouch = System.currentTimeMillis()
        return super.dispatchTouchEvent(ev)
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 27) { setShowWhenLocked(true); setTurnScreenOn(true) }
        web = WebView(this)
        web.setBackgroundColor(Color.BLACK)
        setContentView(web)
        fullScreen()

        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            setSupportMultipleWindows(true)
            userAgentString = "$userAgentString ${Link.UA_TAG}"
        }
        CookieManager.getInstance().setAcceptCookie(true)
        web.addJavascriptInterface(Bridge(), "LinerNotesApp")
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean = openOutside(request.url)
            @Deprecated("old WebView")
            override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean = openOutside(Uri.parse(url))
        }
        web.webChromeClient = object : WebChromeClient() {
            // links that open a new tab (Spotify, Discogs, Buy me a coffee...) open in the phone's browser
            override fun onCreateWindow(view: WebView, isDialog: Boolean, isUserGesture: Boolean, resultMsg: Message): Boolean {
                val probe = WebView(this@MainActivity)
                probe.webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(v: WebView, request: WebResourceRequest): Boolean {
                        openExternal(request.url); v.destroy(); return true
                    }
                }
                (resultMsg.obj as WebView.WebViewTransport).webView = probe
                resultMsg.sendToTarget()
                return true
            }
        }
        web.setDownloadListener { url, _, _, _, _ -> openExternal(Uri.parse(url)) }

        LinkService.onState = { j -> main.post { onPcState(j) } }
        showSetup()
        connect()
        main.post(updateCheck)
        Pro.start(this)
    }

    /** Look for a new version of the app every 15 minutes while it's open (the page asks whether to install it). */
    private val updateCheck = object : Runnable {
        override fun run() {
            io.execute { Updater.check(this@MainActivity) }
            main.postDelayed(this, 15 * 60_000L)
        }
    }

    // ---------------------------------------------------------------- finding and connecting to the PC
    /**
     * At start: Liner Notes on a PC this phone knows (the PC used last time, or one over USB) if it's on.
     * Otherwise the phone works on its own right away, and keeps an eye out for a PC: over USB, or on the
     * same Wi-Fi (a PC it hasn't connected to before asks once, on the PC, whether to connect this phone).
     */
    private fun connect(auto: Boolean = true) {
        say(JSONObject().put("view", if (auto) "starting" else "searching"))
        io.execute {
            for (u in listOfNotNull(Link.savedUrl(this), Link.USB_URL).distinct()) {
                if (Link.alive(this, u)) {
                    if (Link.trusted(this, u)) { main.post { open(u) }; return@execute }
                    if (!auto) { main.post { startPairing(u, null) }; return@execute }
                }
            }
            main.post { if (auto) goSolo() else search() }
        }
    }

    /** The person asked to connect to a PC: list the ones on the Wi-Fi. */
    private fun search() {
        found.clear()
        say(JSONObject().put("view", "searching"))
        finder?.stop()
        val gen = Any(); searchGen = gen
        finder = Finder(this) { name, url ->
            if (server != null || pairing != null || searchGen !== gen) return@Finder
            found[url] = name
            say(JSONObject().put("view", "found").put("list", JSONArray(found.map { JSONObject().put("name", it.value).put("url", it.key) })))
        }.also { it.start() }
        // the backup search, for networks that block the usual one: every address on the Wi-Fi
        Thread {
            Sweep.find(this) { name, url ->
                if (server != null || pairing != null || searchGen !== gen || found.containsKey(url)) return@find
                found[url] = name
                say(JSONObject().put("view", "found").put("list", JSONArray(found.map { JSONObject().put("name", it.value).put("url", it.key) })))
            }
        }.start()
        // nothing after a while: show the help
        main.postDelayed({ if (server == null && pairing == null && found.isEmpty() && searchGen === gen) say(JSONObject().put("view", "notfound")) }, 9000)
    }
    private var searchGen: Any? = null

    // ---------------------------------------------------------------- on its own: the engine on this phone
    private fun goSolo() {
        finder?.stop()
        searchGen = null
        pairing = null
        if (server == null) say(JSONObject().put("view", "starting"))
        Engine.start(this)
        io.execute {
            val ok = Engine.waitReady(this)
            // On its own the phone doesn't listen until you turn listening on (the switch by "Listening is off")
            if (ok) Link.call(this, Engine.URL, "/api/config", "POST", """{"listening_paused":true}""")
            main.post {
                if (!ok) { say(JSONObject().put("view", "error").put("msg", "Liner Notes couldn't start on this phone.")); return@post }
                open(Engine.URL)
            }
        }
    }

    /**
     * While on its own: every 5 s, is a PC there over USB (cheap: it's this phone's own address)?
     * Every 2 minutes, also the PC used last time and a look around the Wi-Fi.
     */
    private var lookTick = 0
    private val asked = mutableSetOf<String>()          // PCs asked this session whether to connect
    private var bgFinder: Finder? = null
    private var stayOwn = false
    private val lookForPc = object : Runnable {
        override fun run() {
            if (!Engine.isLocal(server) || stayOwn) return
            val wide = lookTick++ % 24 == 0
            io.execute {
                val c = this@MainActivity
                val urls = if (wide) listOfNotNull(Link.USB_URL, Link.savedUrl(c)).distinct() else listOf(Link.USB_URL)
                for (u in urls) {
                    if (Link.alive(c, u) && Link.trusted(c, u)) {
                        main.post { if (Engine.isLocal(server)) open(u) }
                        return@execute
                    }
                }
                main.post {
                    if (wide) lookOnWifi()
                    if (Engine.isLocal(server)) main.postDelayed(this, 5_000)
                }
            }
        }
    }

    private fun lookOnWifi() {
        bgFinder?.stop()
        if (stayOwn) return
        val onPc: (String, String) -> Unit = onPc@{ name, url ->
            if (!Engine.isLocal(server)) return@onPc
            io.execute {
                val c = this@MainActivity
                if (!Link.alive(c, url)) return@execute
                if (Link.token(c, url) != null && Link.trusted(c, url)) { main.post { if (Engine.isLocal(server)) open(url) }; return@execute }
                // A PC on the Wi-Fi this phone hasn't connected to: ask it once (the PC shows Connect / Not now)
                val host = Link.host(url)
                if (host in asked || Link.declined(c, host)) return@execute
                asked.add(host)
                val p = Pairing.start(c, url) ?: return@execute
                main.post { toastJs("Liner Notes on $name found. Click Connect on the PC to use it with this phone.") }
                val end = System.currentTimeMillis() + 5 * 60_000
                while (System.currentTimeMillis() < end && Engine.isLocal(server)) {
                    when (Pairing.check(c, url, p.id)) {
                        "approved" -> { main.post { if (Engine.isLocal(server)) open(url) }; return@execute }
                        "denied" -> { Link.setDeclined(c, host); return@execute }
                        "expired" -> return@execute
                    }
                    Thread.sleep(2000)
                }
            }
        }
        val f = Finder(this, onPc)
        bgFinder = f
        f.start()
        main.postDelayed({ f.stop() }, 15_000)
        // the backup search, for networks that block the usual one (a PC it already knows, or a new one to ask)
        Thread { Sweep.find(this, onFound = onPc) }.start()
    }

    /**
     * Keep the shelf the same on the phone and the PC, both ways, without a word: 30 s after the phone
     * switches to the PC, then every 20 minutes while it's on it. The phone's own engine starts quietly
     * for this if it isn't running (it doesn't listen; nothing sends it sound while the PC is in use).
     */
    @Volatile private var syncing = false
    private val syncTick = object : Runnable {
        override fun run() {
            val pc = server
            if (pc == null || Engine.isLocal(pc)) return
            if (!syncing) {
                syncing = true
                Thread({
                    try { syncShelf(pc) } catch (e: Exception) { android.util.Log.w("LinerNotes", "shelf sync", e) }
                    syncing = false
                }, "shelf-sync").start()
            }
            main.postDelayed(this, 20 * 60_000L)
        }
    }

    private fun syncShelf(pc: String) {
        val there = Link.call(this, pc, "/api/sync", timeoutMs = 10_000)?.takeIf { it.first == 200 } ?: return   // an older PC version: skip
        Engine.start(this)
        if (!Engine.waitReady(this)) return
        val here = Link.call(this, Engine.URL, "/api/sync", timeoutMs = 10_000)?.takeIf { it.first == 200 } ?: return
        Link.call(this, Engine.URL, "/api/sync", "POST", there.second, timeoutMs = 20_000)
        Link.call(this, pc, "/api/sync", "POST", here.second, timeoutMs = 20_000)
    }

    /** A short message on the page (the page shows it like its own). */
    private fun toastJs(msg: String) {
        web.evaluateJavascript("window.toast && toast(${JSONObject.quote(msg)})", null)
    }

    /** The PC stopped answering: after a minute, carry on without it. */
    private val pcGone = Runnable {
        val s = server
        if (s != null && !Engine.isLocal(s) && !LinkService.reachable) goSolo()
    }

    private fun pick(url: String, name: String?) {
        finder?.stop()
        io.execute {
            if (!Link.alive(this, url)) { main.post { say(JSONObject().put("view", "error").put("msg", "Couldn't reach Liner Notes at that address.")) }; return@execute }
            if (Link.trusted(this, url)) main.post { open(url) } else main.post { startPairing(url, name) }
        }
    }

    private fun startPairing(url: String, name: String?) {
        finder?.stop()
        io.execute {
            val p = Pairing.start(this, url)
            main.post {
                if (p == null) { say(JSONObject().put("view", "error").put("msg", "Liner Notes on the PC didn't answer. Is it up to date?")); return@post }
                pairing = p; pairUrl = url
                say(JSONObject().put("view", "pair").put("code", p.code).put("name", name ?: found[url] ?: "your PC"))
                pollPairing()
            }
        }
    }

    private fun pollPairing() {
        val p = pairing ?: return
        val url = pairUrl ?: return
        io.execute {
            val state = Pairing.check(this, url, p.id)
            main.post {
                if (pairing !== p) return@post
                when (state) {
                    "approved" -> { pairing = null; open(url) }
                    "denied" -> { pairing = null; say(JSONObject().put("view", "denied")) }
                    "expired" -> { pairing = null; say(JSONObject().put("view", "error").put("msg", "That took too long. Try again.")) }
                    else -> main.postDelayed({ pollPairing() }, 1500)
                }
            }
        }
    }

    private fun open(url: String) {
        finder?.stop()
        searchGen = null
        server = url
        main.removeCallbacks(lookForPc); main.removeCallbacks(pcGone); main.removeCallbacks(syncTick)
        if (!Engine.isLocal(url)) main.postDelayed(syncTick, 30_000)
        if (Engine.isLocal(url)) { lookTick = 1; main.postDelayed(lookForPc, 5_000); main.postDelayed({ lookOnWifi() }, 3_000) }
        else bgFinder?.stop()
        if (!Engine.isLocal(url)) { Link.saveUrl(this, url); stayOwn = false }
        Link.token(this, url)?.let { t ->
            CookieManager.getInstance().setCookie(url, "ln_device=$t; Max-Age=315360000; Path=/")
            CookieManager.getInstance().flush()
        }
        web.loadUrl(url.trimEnd('/') + "/")
        askNotifications()
        LinkService.start(this, url)
    }

    private fun showSetup() {
        server = null
        web.loadUrl("file:///android_asset/setup.html")
    }

    /** Send the setup page what to show (it may still be loading: it asks for the latest state when ready). */
    private var lastSay: JSONObject? = null
    private fun say(state: JSONObject) {
        lastSay = state
        if (server == null) web.evaluateJavascript("window.ln && ln.show(${JSONObject.quote(state.toString())})", null)
    }

    // ---------------------------------------------------------------- the PC's state, from LinkService
    private fun onPcState(j: JSONObject?) {
        if (server == null) return
        if (j == null && !Engine.isLocal(server)) { main.removeCallbacks(pcGone); main.postDelayed(pcGone, 40_000) }
        if (j != null) {
            val micOn = j.optJSONObject("mic")?.optString("mode", "off") != "off"
            if (micOn && !askedMic && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                askedMic = true
                requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 2)   // the PC listens through this phone
            }
        }
        if (LinkService.screenOffWanted) { if (!dark) goDark() }
        else if (j != null && j.optJSONObject("track")?.optString("status") == "playing") {
            // A song is playing: the screen stays on, whatever the page did (on a plain charger nothing else holds it)
            if (dark || !keptOn) keepOn(true)
        }
    }

    private fun askNotifications() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 2 && grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            server?.let { LinkService.stop(this); LinkService.start(this, it) }      // restart so it may use the mic
        }
    }

    // ---------------------------------------------------------------- the screen
    private var keptOn = false
    private fun keepOn(on: Boolean) {
        keptOn = on
        if (on) {
            dark = false
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            window.attributes = window.attributes.apply { screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE }
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    /** Nothing playing for a while: go black and let the phone's own screen timeout switch it off. */
    private fun goDark() {
        dark = true
        keepOn(false)
        window.attributes = window.attributes.apply { screenBrightness = 0f }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (!LinkService.screenOffWanted) { keepOn(true) }   // LinkService woke the screen for the next song
    }

    override fun onResume() {
        super.onResume()
        fullScreen()
        if (dark && !LinkService.screenOffWanted) keepOn(true)
        io.execute { Updater.check(this) }
        Pro.refresh()                    // bought, renewed or cancelled while the app was away
    }

    @Suppress("DEPRECATION")
    private fun fullScreen() {
        if (Build.VERSION.SDK_INT >= 30) {
            window.setDecorFitsSystemWindows(false)
            window.insetsController?.let {
                it.hide(WindowInsets.Type.statusBars() or WindowInsets.Type.navigationBars())
                it.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_FULLSCREEN or
                    View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
                    View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN)
        }
    }

    // ---------------------------------------------------------------- links, back button, sharing
    private fun openOutside(uri: Uri): Boolean {
        val s = server
        if (uri.scheme == "file") return false
        if (s != null && (uri.scheme == "http" || uri.scheme == "https") && uri.host == Link.host(s)) return false
        openExternal(uri)
        return true
    }

    private fun openExternal(uri: Uri) {
        try { startActivity(Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) } catch (e: Exception) { }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (server == null) { super.onBackPressed(); return }
        // close an open page (Settings, a song...) first, like Escape on the PC
        web.evaluateJavascript("(function(){var p=document.querySelector('.panel.open');if(p){p.classList.remove('open');return 1}return 0})()") { r ->
            if (r == "1") return@evaluateJavascript
            AlertDialog.Builder(this)
                .setTitle("Liner Notes")
                .setItems(arrayOf("Keep showing it", if (Engine.isLocal(server)) "Connect to a PC" else "Connect to a different PC", "Close the app")) { _, which ->
                    when (which) {
                        1 -> switchPc()
                        2 -> { LinkService.stop(this); finish() }
                    }
                }.show()
        }
    }

    private fun switchPc() {
        LinkService.stop(this)
        main.removeCallbacks(lookForPc); main.removeCallbacks(pcGone); main.removeCallbacks(syncTick)
        if (!Engine.isLocal(server)) Link.forget(this)
        showSetup(); search()
    }

    /** The engine's log, to send when something goes wrong on the phone on its own. */
    private fun shareLogFile() {
        try {
            val src = File(File(filesDir, "Liner Notes"), "linernotes.log")
            if (!src.exists()) { main.post { toastJs("There's no log yet.") }; return }
            val dir = File(cacheDir, "shared").apply { mkdirs() }
            val f = File(dir, "liner-notes-phone-log.txt")
            // the last 400 KB is plenty
            val bytes = src.readBytes()
            f.writeBytes(if (bytes.size > 400_000) bytes.copyOfRange(bytes.size - 400_000, bytes.size) else bytes)
            val uri = FileProvider.getUriForFile(this, "$packageName.files", f)
            val i = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM, uri)
                .putExtra(Intent.EXTRA_SUBJECT, "Liner Notes phone log").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            main.post { startActivity(Intent.createChooser(i, "Send the log").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
        } catch (e: Exception) { main.post { toastJs("Couldn't share the log.") } }
    }

    private fun shareText(title: String, text: String, url: String) {
        val i = Intent(Intent.ACTION_SEND).setType("text/plain")
            .putExtra(Intent.EXTRA_SUBJECT, title)
            .putExtra(Intent.EXTRA_TEXT, "$text $url")
        startActivity(Intent.createChooser(i, "Share").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    private fun shareImage(url: String, name: String) {
        io.execute {
            try {
                val dir = File(cacheDir, "shared").apply { mkdirs() }
                val f = File(dir, name.replace(Regex("[^\\w.\\-]"), "_"))
                val conn = URL(url).openConnection()
                server?.let { s -> Link.token(this, s)?.let { conn.setRequestProperty("X-LinerNotes-Device", it) } }
                conn.getInputStream().use { inp -> f.outputStream().use { inp.copyTo(it) } }
                val uri = FileProvider.getUriForFile(this, "$packageName.files", f)
                val i = Intent(Intent.ACTION_SEND).setType("image/png").putExtra(Intent.EXTRA_STREAM, uri)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                main.post { startActivity(Intent.createChooser(i, "Share").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
            } catch (e: Exception) { }
        }
    }

    /** What the page (setup page or the PC's screen) can ask the app to do. */
    inner class Bridge {
        @JavascriptInterface fun keepAwake(on: Boolean) = main.post {
            if (on) {
                // A page that just loaded (say, the phone carrying on by itself after the PC went to sleep) asks too:
                // while the screen is meant to be dark, only a tap on it wakes it.
                if (LinkService.screenOffWanted && System.currentTimeMillis() - lastTouch > 5_000) return@post
                LinkService.screenOffWanted = false; keepOn(true)
            } else keepOn(false)
        }.let { }
        @JavascriptInterface fun screenOff() = main.post { goDark() }.let { }
        @JavascriptInterface fun share(title: String?, text: String?, url: String?) = main.post { shareText(title ?: "", text ?: "", url ?: "") }.let { }
        @JavascriptInterface fun shareImage(url: String, name: String) = this@MainActivity.shareImage(url, name)
        @JavascriptInterface fun openUrl(url: String) = main.post { if (url.startsWith("https://")) openExternal(Uri.parse(url)) }.let { }
        @JavascriptInterface fun shareLog() = io.execute { shareLogFile() }
        @JavascriptInterface fun mode(): String = if (Engine.isLocal(server)) "solo" else "pc"
        // the person chose the phone on its own: don't switch to a PC by itself until the app is opened again
        @JavascriptInterface fun solo() = main.post { stayOwn = true; LinkService.stop(this@MainActivity); goSolo() }.let { }
        @JavascriptInterface fun switchPc() = main.post { this@MainActivity.switchPc() }.let { }
        // updates for the copy installed from GitHub
        @JavascriptInterface fun appUpdate(): String = Updater.json(this@MainActivity)
        // Liner Notes Plus (Google Play copy): what's unlocked, and buying it
        @JavascriptInterface fun pro(): String = Pro.json(this@MainActivity)
        @JavascriptInterface fun buyPro(plan: String) = main.post { Pro.buy(this@MainActivity, plan) }.let { }
        @JavascriptInterface fun restorePro() = Pro.refresh()
        @JavascriptInterface fun checkAppUpdate() = io.execute { Updater.check(this@MainActivity, force = true) }
        @JavascriptInterface fun installAppUpdate() { Thread { Updater.install(this@MainActivity) { r -> main.post(r) } }.start() }
        @JavascriptInterface fun version(): String = try { packageManager.getPackageInfo(packageName, 0).versionName ?: "" } catch (e: Exception) { "" }
        // setup page
        @JavascriptInterface fun ready() = main.post { lastSay?.let { say(it) } }.let { }
        @JavascriptInterface fun retry() = main.post { pairing = null; connect(auto = false) }.let { }
        @JavascriptInterface fun connectTo(url: String, name: String?) = main.post { pick(url, name) }.let { }
        @JavascriptInterface fun manual(address: String) = main.post {
            var a = address.trim()
            if (a.isEmpty()) return@post
            if (!a.startsWith("http")) a = "http://$a"
            if (Uri.parse(a).port == -1) a = a.trimEnd('/') + ":8765"
            pick(a, null)
        }.let { }
    }

    override fun onDestroy() {
        finder?.stop()
        LinkService.onState = null
        super.onDestroy()
    }
}
