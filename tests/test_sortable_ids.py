"""
Tests for sortable ID strategies (ULID/KSUID) in AutoKeyField.

This module tests the strategy parameter for AutoKeyField, which allows
choosing between UUID4 (default), ULID, and KSUID ID generation strategies.
"""

import pytest
import time


class TestUUID4Strategy:
    """Tests for the default UUID4 strategy."""

    def test_default_strategy_is_uuid4(self):
        """AutoKeyField should default to uuid4 strategy."""
        import popoto

        class DefaultModel(popoto.Model):
            uuid = popoto.AutoKeyField()
            name = popoto.Field()

        # Check that the field has uuid4 strategy
        assert DefaultModel._meta.fields["uuid"].strategy == "uuid4"

    def test_uuid4_generates_32_char_hex(self):
        """UUID4 strategy should generate 32-character hex string."""
        import popoto

        class UUIDModel(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="uuid4")
            name = popoto.Field()

        instance = UUIDModel(name="test")
        assert len(instance.uuid) == 32
        # Verify it's a valid hex string
        int(instance.uuid, 16)  # Should not raise

    def test_uuid4_custom_length(self):
        """UUID4 strategy should respect auto_uuid_length parameter."""
        import popoto

        class ShortUUIDModel(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="uuid4", auto_uuid_length=16)
            name = popoto.Field()

        instance = ShortUUIDModel(name="test")
        assert len(instance.uuid) == 16

    def test_uuid4_backward_compatibility(self):
        """Models without explicit strategy should work as before."""
        import popoto

        class LegacyModel(popoto.Model):
            uuid = popoto.AutoKeyField()
            name = popoto.Field()

        instance = LegacyModel(name="test")
        # Should generate a valid 32-char hex ID
        assert len(instance.uuid) == 32
        int(instance.uuid, 16)  # Should not raise

    def test_uuid4_uniqueness(self):
        """UUID4 should generate unique IDs."""
        import popoto

        class UniqueModel(popoto.Model):
            uuid = popoto.AutoKeyField()
            name = popoto.Field()

        ids = set()
        for i in range(100):
            instance = UniqueModel(name=f"test_{i}")
            ids.add(instance.uuid)

        assert len(ids) == 100  # All IDs should be unique


class TestULIDStrategy:
    """Tests for the ULID strategy."""

    def test_ulid_strategy_generates_26_chars(self):
        """ULID strategy should generate 26-character string."""
        pytest.importorskip("ulid", reason="ulid-py not installed")
        import popoto

        class ULIDModel(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ulid")
            name = popoto.Field()

        instance = ULIDModel(name="test")
        assert len(instance.uuid) == 26

    def test_ulid_is_time_sortable(self):
        """ULID IDs generated later should sort after earlier ones."""
        pytest.importorskip("ulid", reason="ulid-py not installed")
        import popoto

        class TimeSortedModel(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ulid")
            name = popoto.Field()

        # Generate IDs with small delays
        ids = []
        for i in range(5):
            instance = TimeSortedModel(name=f"test_{i}")
            ids.append(instance.uuid)
            time.sleep(0.002)  # Small delay to ensure different timestamps

        # Sorted order should match generation order
        assert ids == sorted(ids)

    def test_ulid_uniqueness(self):
        """ULID should generate unique IDs."""
        pytest.importorskip("ulid", reason="ulid-py not installed")
        import popoto

        class UniqueULIDModel(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ulid")
            name = popoto.Field()

        ids = set()
        for i in range(100):
            instance = UniqueULIDModel(name=f"test_{i}")
            ids.add(instance.uuid)

        assert len(ids) == 100

    def test_ulid_import_error_message(self):
        """Should raise ImportError with helpful message when ulid-py not installed."""
        import popoto
        import sys
        from unittest.mock import patch

        # Create a field directly and test ID generation with mocked import
        field = popoto.AutoKeyField(strategy="ulid")

        # Mock import failure
        original_modules = sys.modules.copy()
        # Remove ulid from modules temporarily
        ulid_mod = sys.modules.pop("ulid", None)

        try:
            # Patch the import to fail
            def mock_import(name, *args, **kwargs):
                if name == "ulid":
                    raise ImportError("No module named 'ulid'")
                return original_modules.get(name) or __import__(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                with pytest.raises(ImportError) as exc_info:
                    field.get_new_auto_key_value()
                assert "ulid-py" in str(exc_info.value)
                assert "pip install ulid-py" in str(exc_info.value)
        finally:
            # Restore ulid module if it was installed
            if ulid_mod:
                sys.modules["ulid"] = ulid_mod


class TestKSUIDStrategy:
    """Tests for the KSUID strategy."""

    def test_ksuid_strategy_generates_27_chars(self):
        """KSUID strategy should generate 27-character string."""
        pytest.importorskip("cyksuid", reason="cyksuid not installed")
        import popoto

        class KSUIDModel(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ksuid")
            name = popoto.Field()

        instance = KSUIDModel(name="test")
        assert len(instance.uuid) == 27

    def test_ksuid_is_time_sortable(self):
        """KSUID IDs generated with >1s apart should sort after earlier ones.

        Note: KSUID has 1-second timestamp granularity, so IDs generated within
        the same second may not be strictly sortable. This test uses 1.1s delays
        to ensure different timestamps.
        """
        pytest.importorskip("cyksuid", reason="cyksuid not installed")
        import popoto

        class TimeSortedKSUID(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ksuid")
            name = popoto.Field()

        # Generate IDs with delays >1s to ensure different timestamps
        # (KSUID has 1-second timestamp granularity)
        ids = []
        for i in range(3):
            instance = TimeSortedKSUID(name=f"test_{i}")
            ids.append(instance.uuid)
            if i < 2:  # Don't sleep after last iteration
                time.sleep(1.1)

        # Sorted order should match generation order
        assert ids == sorted(ids)

    def test_ksuid_uniqueness(self):
        """KSUID should generate unique IDs."""
        pytest.importorskip("cyksuid", reason="cyksuid not installed")
        import popoto

        class UniqueKSUIDModel(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ksuid")
            name = popoto.Field()

        ids = set()
        for i in range(100):
            instance = UniqueKSUIDModel(name=f"test_{i}")
            ids.add(instance.uuid)

        assert len(ids) == 100

    def test_ksuid_import_error_message(self):
        """Should raise ImportError with helpful message when cyksuid not installed."""
        import popoto
        import sys
        from unittest.mock import patch

        # Create a field directly and test ID generation with mocked import
        field = popoto.AutoKeyField(strategy="ksuid")

        # Mock import failure
        original_modules = sys.modules.copy()
        # Remove cyksuid from modules temporarily
        cyksuid_mod = sys.modules.pop("cyksuid", None)
        cyksuid_ksuid_mod = sys.modules.pop("cyksuid.ksuid", None)

        try:

            def mock_import(name, *args, **kwargs):
                if name.startswith("cyksuid"):
                    raise ImportError("No module named 'cyksuid'")
                return original_modules.get(name) or __import__(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                with pytest.raises(ImportError) as exc_info:
                    field.get_new_auto_key_value()
                assert "cyksuid" in str(exc_info.value)
                assert "pip install cyksuid" in str(exc_info.value)
        finally:
            # Restore cyksuid modules if they were installed
            if cyksuid_mod:
                sys.modules["cyksuid"] = cyksuid_mod
            if cyksuid_ksuid_mod:
                sys.modules["cyksuid.ksuid"] = cyksuid_ksuid_mod


class TestInvalidStrategy:
    """Tests for invalid strategy handling."""

    def test_invalid_strategy_raises_error(self):
        """Invalid strategy should raise ValueError."""
        import popoto

        with pytest.raises(ValueError) as exc_info:

            class InvalidModel(popoto.Model):
                uuid = popoto.AutoKeyField(strategy="invalid")
                name = popoto.Field()

        assert "Invalid strategy" in str(exc_info.value)
        assert "invalid" in str(exc_info.value)
        assert "uuid4" in str(exc_info.value)

    def test_error_message_lists_valid_strategies(self):
        """Error message should list all valid strategies."""
        import popoto

        with pytest.raises(ValueError) as exc_info:

            class InvalidModel2(popoto.Model):
                uuid = popoto.AutoKeyField(strategy="nanoid")
                name = popoto.Field()

        error_msg = str(exc_info.value)
        assert "uuid4" in error_msg
        assert "ulid" in error_msg
        assert "ksuid" in error_msg


class TestValidation:
    """Tests for ID validation."""

    def test_uuid4_validation(self):
        """UUID4 IDs should pass validation."""
        import popoto

        class ValidUUID(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="uuid4")
            name = popoto.Field()

        instance = ValidUUID(name="test")
        field = ValidUUID._meta.fields["uuid"]
        assert field.is_valid(field, instance.uuid)

    def test_ulid_validation(self):
        """ULID IDs should pass validation."""
        pytest.importorskip("ulid", reason="ulid-py not installed")
        import popoto

        class ValidULID(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ulid")
            name = popoto.Field()

        instance = ValidULID(name="test")
        field = ValidULID._meta.fields["uuid"]
        assert field.is_valid(field, instance.uuid)

    def test_ksuid_validation(self):
        """KSUID IDs should pass validation."""
        pytest.importorskip("cyksuid", reason="cyksuid not installed")
        import popoto

        class ValidKSUID(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ksuid")
            name = popoto.Field()

        instance = ValidKSUID(name="test")
        field = ValidKSUID._meta.fields["uuid"]
        assert field.is_valid(field, instance.uuid)

    def test_wrong_length_fails_validation(self):
        """IDs with wrong length should fail validation."""
        import popoto

        class LengthCheck(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="uuid4")
            name = popoto.Field()

        field = LengthCheck._meta.fields["uuid"]
        # A 26-char string (ULID length) should fail for uuid4 strategy
        assert not field.is_valid(field, "a" * 26)


class TestModelPersistence:
    """Tests for model save/load with different strategies."""

    def test_uuid4_save_and_load(self):
        """Models with UUID4 strategy should save and load correctly."""
        import popoto

        class PersistUUID(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="uuid4")
            name = popoto.Field()

        instance = PersistUUID(name="test_uuid4")
        instance.save()

        loaded = PersistUUID.query.get(uuid=instance.uuid)
        assert loaded is not None
        assert loaded.uuid == instance.uuid
        assert loaded.name == "test_uuid4"

        # Cleanup
        instance.delete()

    def test_ulid_save_and_load(self):
        """Models with ULID strategy should save and load correctly."""
        pytest.importorskip("ulid", reason="ulid-py not installed")
        import popoto

        class PersistULID(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ulid")
            name = popoto.Field()

        instance = PersistULID(name="test_ulid")
        instance.save()

        loaded = PersistULID.query.get(uuid=instance.uuid)
        assert loaded is not None
        assert loaded.uuid == instance.uuid
        assert loaded.name == "test_ulid"

        # Cleanup
        instance.delete()

    def test_ksuid_save_and_load(self):
        """Models with KSUID strategy should save and load correctly."""
        pytest.importorskip("cyksuid", reason="cyksuid not installed")
        import popoto

        class PersistKSUID(popoto.Model):
            uuid = popoto.AutoKeyField(strategy="ksuid")
            name = popoto.Field()

        instance = PersistKSUID(name="test_ksuid")
        instance.save()

        loaded = PersistKSUID.query.get(uuid=instance.uuid)
        assert loaded is not None
        assert loaded.uuid == instance.uuid
        assert loaded.name == "test_ksuid"

        # Cleanup
        instance.delete()
