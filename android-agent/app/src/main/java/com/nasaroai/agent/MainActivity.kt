package com.nasaroai.agent

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var pendingOpenAgent = false

    companion object {
        private const val PREFS = "nasaro_app"
        private const val KEY_SERVER = "server_url"
    }

    private fun resolveServerUrl(): String {
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        var url = prefs.getString(KEY_SERVER, null)?.trim().orEmpty()
        if (url.isEmpty()) {
            url = intent?.getStringExtra("server_url")?.trim().orEmpty()
        }
        if (url.isEmpty()) return ""
        if (!url.startsWith("http")) url = "https://$url"
        return url.trimEnd('/')
    }

    private fun saveServerUrl(url: String) {
        getSharedPreferences(PREFS, MODE_PRIVATE)
            .edit()
            .putString(KEY_SERVER, url.trimEnd('/'))
            .apply()
    }

    private fun loadAppOrConfig() {
        val base = resolveServerUrl()
        if (base.isEmpty()) {
            webView.loadDataWithBaseURL(
                null,
                """
                <!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <style>body{font-family:sans-serif;background:#0f0a1a;color:#f3f4f6;padding:24px;line-height:1.6}
                input,button{font-size:16px;padding:10px;border-radius:8px;border:1px solid #4c1d95}
                button{background:#7c3aed;color:#fff;border:0;margin-top:8px;width:100%}</style></head>
                <body>
                <h2>Nasaro AI 서버 주소</h2>
                <p>Railway 배포 URL을 입력하세요.<br>(Variables의 <code>PUBLIC_APP_URL</code> 또는 <code>*.up.railway.app</code> 도메인)</p>
                <input id="url" type="url" placeholder="https://your-app.up.railway.app" style="width:100%;box-sizing:border-box">
                <button onclick="save()">연결</button>
                <script>
                function save(){
                  var u=document.getElementById('url').value.trim();
                  if(!u)return;
                  if(!u.startsWith('http')) u='https://'+u;
                  NasaroAndroidAgent.setServerUrl(u);
                }
                </script>
                </body></html>
                """.trimIndent(),
                "text/html",
                "UTF-8",
                null
            )
            return
        }
        webView.loadUrl("$base/?source=app")
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.mainWebView)
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                return false
            }
        }
        webView.webChromeClient = WebChromeClient()
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = WebSettings.LOAD_DEFAULT
            userAgentString = userAgentString + " NasaroAIApp"
        }
        webView.addJavascriptInterface(AndroidAgentBridge(this), "NasaroAndroidAgent")

        loadAppOrConfig()

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })
    }

    override fun onResume() {
        super.onResume()
        if (pendingOpenAgent && Settings.canDrawOverlays(this)) {
            pendingOpenAgent = false
            openNativeAgentOverlay()
        }
    }

    private fun requestOverlayPermission() {
        pendingOpenAgent = true
        startActivity(
            Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:$packageName")
            )
        )
    }

    private fun openNativeAgentOverlay() {
        getSharedPreferences("nasaroai_float", MODE_PRIVATE)
            .edit()
            .putBoolean("agent_enabled", true)
            .apply()

        val intent = Intent(this, FloatingService::class.java).apply {
            action = FloatingService.ACTION_OPEN_PANEL
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }

    inner class AndroidAgentBridge(private val context: Context) {
        @JavascriptInterface
        fun setServerUrl(url: String) {
            runOnUiThread {
                saveServerUrl(url)
                loadAppOrConfig()
            }
        }

        @JavascriptInterface
        fun openAgent() {
            runOnUiThread {
                if (Settings.canDrawOverlays(context)) {
                    openNativeAgentOverlay()
                } else {
                    requestOverlayPermission()
                }
            }
        }

        @JavascriptInterface
        fun openAccessibilitySettings() {
            runOnUiThread {
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
        }

        @JavascriptInterface
        fun openOverlaySettings() {
            runOnUiThread {
                startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:$packageName")
                    )
                )
            }
        }

        @JavascriptInterface
        fun canDrawOverlay(): Boolean {
            return Settings.canDrawOverlays(context)
        }
    }
}
