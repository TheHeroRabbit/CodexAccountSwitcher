import unittest

from app import (
    TABLE_CELL_INSET,
    bounded_confirm_height,
    bounded_notice_height,
    format_table_cell,
    preferred_window_size,
    table_column_widths,
)


class NoticeDialogLayoutTests(unittest.TestCase):
    def test_confirm_dialog_uses_compact_safe_minimum(self) -> None:
        self.assertEqual(bounded_confirm_height(180, 1080), 225)

    def test_notice_height_keeps_existing_minimum(self) -> None:
        self.assertEqual(bounded_notice_height(220, 1080), 265)

    def test_notice_height_expands_for_wrapped_content(self) -> None:
        self.assertEqual(bounded_notice_height(342, 1080), 342)

    def test_notice_height_stays_within_visible_screen(self) -> None:
        self.assertEqual(bounded_notice_height(900, 768), 672)

    def test_main_window_uses_preferred_size_on_large_screen(self) -> None:
        self.assertEqual(preferred_window_size(1920, 1080), (1080, 720))

    def test_main_window_fits_common_small_screen(self) -> None:
        self.assertEqual(preferred_window_size(1366, 768), (1080, 672))

    def test_main_window_has_a_safe_lower_bound(self) -> None:
        self.assertEqual(preferred_window_size(800, 600), (760, 560))

    def test_table_cell_formatter_adds_a_consistent_inset(self) -> None:
        self.assertEqual(format_table_cell("工作账号"), TABLE_CELL_INSET + "工作账号")
        self.assertEqual(format_table_cell(""), TABLE_CELL_INSET)

    def test_table_columns_fit_the_visible_width(self) -> None:
        widths = table_column_widths(478)
        self.assertEqual(widths, (232, 84, 160))
        self.assertLessEqual(sum(widths), 478)

    def test_table_remark_column_absorbs_extra_width(self) -> None:
        self.assertEqual(table_column_widths(660), (414, 84, 160))


if __name__ == "__main__":
    unittest.main()
