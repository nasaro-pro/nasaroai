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

        // 앱에서는 설치 팝업이 뜨지 않도록 source=app 전달
        webView.loadUrl("https://nasaroai.onrender.com/?source=app")

        // 앱 시작만으로는 떠 있는 런처를 만들지 않는다.
        // 사용자가 웹의 "에이전트" 버튼을 눌렀을 때만 네이티브 오버레이를 연다.

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
    }
}
