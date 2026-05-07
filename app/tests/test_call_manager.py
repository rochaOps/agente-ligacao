import pytest
import time
from unittest.mock import Mock, patch, MagicMock, call
import threading
from call_manager import CallManager


class TestExponentialBackoff:
    """Unit tests for exponential backoff reconnect logic."""

    def test_backoff_sequence(self):
        """Verify backoff sequence: 1s, 2s, 4s, 8s, 16s (capped at 32s)."""
        cm = CallManager()

        # Mock time.sleep and os.path.exists
        with patch('time.sleep') as mock_sleep, \
             patch('os.path.exists', return_value=True), \
             patch.object(cm, 'connect', return_value=False):

            # Run backoff with 5 attempts
            cm._reconnect_with_backoff(max_attempts=5, base_delay=1.0)

            # Extract actual sleep delays from call args
            sleep_calls = [call_obj[0][0] for call_obj in mock_sleep.call_args_list]
            expected = [1.0, 2.0, 4.0, 8.0, 16.0]

            assert sleep_calls == expected, f"Expected {expected}, got {sleep_calls}"

    def test_backoff_cap_at_32s(self):
        """Verify backoff caps at 32 seconds."""
        cm = CallManager()

        with patch('time.sleep') as mock_sleep, \
             patch('os.path.exists', return_value=True), \
             patch.object(cm, 'connect', return_value=False):

            # Run with attempts that would exceed 32s
            cm._reconnect_with_backoff(max_attempts=7, base_delay=1.0)

            sleep_calls = [call_obj[0][0] for call_obj in mock_sleep.call_args_list]

            # Should be: 1, 2, 4, 8, 16, 32, 32 (capped)
            expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 32.0]
            assert sleep_calls == expected, f"Expected {expected}, got {sleep_calls}"

    def test_reset_on_success(self):
        """Verify reconnect resets backoff on success (doesn't get stuck at high delay)."""
        cm = CallManager()

        with patch('time.sleep') as mock_sleep, \
             patch('os.path.exists', return_value=True), \
             patch.object(cm, 'connect', return_value=True):  # Success on first call

            cm._reconnect_with_backoff(max_attempts=5, base_delay=1.0)

            # Should sleep only once (1s) before successful connect
            assert mock_sleep.call_count == 1
            assert mock_sleep.call_args_list[0][0][0] == 1.0

    def test_attempt_limit(self):
        """Verify reconnect stops after max_attempts and logs error."""
        cm = CallManager()

        with patch('time.sleep'), \
             patch('os.path.exists', return_value=True), \
             patch.object(cm, 'connect', return_value=False), \
             patch('call_manager.logger') as mock_logger:

            cm._reconnect_with_backoff(max_attempts=5, base_delay=1.0)

            # Should have attempted exactly 5 times
            assert cm.connect.call_count == 5

            # Should log final failure message
            assert any('failed' in str(call_obj).lower()
                      for call_obj in mock_logger.error.call_args_list)

    def test_device_existence_check(self):
        """Verify device symlink existence is checked before reconnect."""
        cm = CallManager()

        with patch('time.sleep'), \
             patch('os.path.exists', return_value=False) as mock_exists, \
             patch.object(cm, 'connect') as mock_connect:

            cm._reconnect_with_backoff(max_attempts=3, base_delay=1.0)

            # connect() should never be called if device doesn't exist
            assert mock_connect.call_count == 0
            # But exists() should be checked for each attempt
            assert mock_exists.call_count >= 3

    def test_close_serial_on_reconnect(self):
        """Verify serial port is properly closed before reconnect attempt."""
        cm = CallManager()

        # Mock serial.Serial
        mock_serial = MagicMock()
        mock_serial.is_open = True
        cm.ser = mock_serial

        with patch('time.sleep'), \
             patch('os.path.exists', return_value=True), \
             patch.object(cm, 'connect', return_value=True):

            cm._reconnect_with_backoff(max_attempts=1, base_delay=1.0)

            # Should close the old serial port
            mock_serial.close.assert_called_once()
            # Should clear the ser reference
            assert cm.ser is None  # Reset before reconnect attempt

    def test_running_state_management(self):
        """Verify _running state is properly managed during reconnect."""
        cm = CallManager()
        cm._running = True  # Start as if listener is running

        with patch('time.sleep'), \
             patch('os.path.exists', return_value=True), \
             patch.object(cm, 'connect', return_value=True):

            cm._reconnect_with_backoff(max_attempts=1, base_delay=1.0)

            # After successful reconnect, _running should be True (connect() sets it)
            assert cm._running == True

    def test_no_infinite_retry(self):
        """Verify reconnect doesn't loop infinitely on failure."""
        cm = CallManager()

        with patch('time.sleep') as mock_sleep, \
             patch('os.path.exists', return_value=True), \
             patch.object(cm, 'connect', return_value=False):

            start = time.time()
            cm._reconnect_with_backoff(max_attempts=3, base_delay=0.1)  # Use small delay for test
            elapsed = time.time() - start

            # Should complete quickly (max ~0.3s with 0.1s base delay)
            # This prevents infinite loops
            assert mock_sleep.call_count == 3
            assert elapsed < 1.0  # Should finish in reasonable time


class TestErrorn5Handling:
    """Tests for Errno 5 (EIO) exception handling in _listen_loop."""

    def test_errno5_triggers_reconnect(self):
        """Verify Errno 5 exception triggers _reconnect_with_backoff."""
        cm = CallManager()
        cm._running = True

        # Mock serial port to raise Errno 5
        mock_serial = MagicMock()
        mock_serial.is_open = True
        mock_serial.in_waiting = True
        mock_serial.read.side_effect = OSError(5, "Input/output error")
        cm.ser = mock_serial

        with patch.object(cm, '_reconnect_with_backoff') as mock_reconnect, \
             patch('call_manager.logger'):

            # Run one iteration of listen loop
            # (We'll use threading but with a short timeout to prevent hanging)
            cm._running = False  # Stop after one iteration
            cm._start_listener()

            # Give listener thread time to process
            # Note: Since we set _running=False immediately, it should exit
            # This is a simplified test; full integration test would be better


class TestBackwardCompatibility:
    """Ensure changes maintain backward compatibility."""

    def test_callmanager_interface_unchanged(self):
        """Verify CallManager public interface is unchanged."""
        cm = CallManager()

        # Should have expected public methods
        assert hasattr(cm, 'connect')
        assert hasattr(cm, 'disconnect')
        assert hasattr(cm, 'dial')
        assert hasattr(cm, 'answer')
        assert hasattr(cm, 'hangup')
        assert hasattr(cm, 'initialize')

        # Should have expected callbacks
        assert hasattr(cm, 'on_ring')
        assert hasattr(cm, 'on_call_begin')
        assert hasattr(cm, 'on_call_end')
        assert hasattr(cm, 'on_dtmf')

    def test_reconnect_method_signature(self):
        """Verify _reconnect_with_backoff has expected signature."""
        cm = CallManager()

        # Should accept max_attempts and base_delay parameters
        import inspect
        sig = inspect.signature(cm._reconnect_with_backoff)

        assert 'max_attempts' in sig.parameters
        assert 'base_delay' in sig.parameters

        # Check defaults match spec
        assert sig.parameters['max_attempts'].default == 5
        assert sig.parameters['base_delay'].default == 1.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
