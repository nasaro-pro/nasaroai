package com.nasaroai.agent

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val prefs = context.getSharedPreferences("nasaroai_float", Context.MODE_PRIVATE)
        if (prefs.getBoolean("auto_start", false)) {
            val svc = Intent(context, FloatingService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                context.startForegroundService(svc)
            else
                context.startService(svc)
        }
    }
}
