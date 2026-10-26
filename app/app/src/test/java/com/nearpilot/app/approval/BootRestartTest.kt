package com.nearpilot.app.approval

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BootRestartTest {

    private val bootCompleted = "android.intent.action.BOOT_COMPLETED"
    private val packageReplaced = "android.intent.action.MY_PACKAGE_REPLACED"

    @Test
    fun FR03_restarts_watch_after_boot() {
        assertTrue(BootRestart.shouldStart(bootCompleted, watching = true))
        assertTrue(BootRestart.shouldStart(packageReplaced, watching = true))

        assertFalse(BootRestart.shouldStart(bootCompleted, watching = false))
        assertFalse(BootRestart.shouldStart(packageReplaced, watching = false))

        assertFalse(BootRestart.shouldStart("android.intent.action.TIME_SET", watching = true))
        assertFalse(BootRestart.shouldStart(null, watching = true))
    }
}
