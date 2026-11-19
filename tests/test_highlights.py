import unittest

from clipmatch.highlight_signals import MediaSample
from clipmatch.highlights import choose_edit


def footage(*, duration=60, actions=(), speech=(), camera=(), background=-32):
    samples = []
    for quarter in range(duration * 4):
        t = quarter / 4
        playing = any(start <= t < end for start, end in actions)
        speaking = any(start <= t < end for start, end in speech)
        reacting = any(start <= t < end for start, end in camera)
        samples.append(MediaSample(t, 32 if playing else 3, 23 if reacting else 2,
                                   -15 if speaking else background))
    return samples


class HighlightTests(unittest.TestCase):
    def test_an_early_or_late_visual_event_moves_the_edit_with_it(self):
        for onset in (7, 28, 48):
            with self.subTest(onset=onset):
                edit = choose_edit(60, footage(actions=[(onset, onset + 5)]), None)
                self.assertLess(edit.start, onset)
                self.assertLessEqual(onset - edit.start, 6)
                self.assertGreaterEqual(edit.end, onset + 6)
                self.assertLessEqual(edit.end - edit.start, 23)

    def test_a_brief_loud_sound_does_not_displace_a_later_combined_reaction(self):
        samples = footage(actions=[(43, 48)], speech=[(4, 4.25), (43.25, 46)],
                          camera=[(43.5, 45)])
        samples[16] = MediaSample(4, 3, 2, -1)
        edit = choose_edit(60, samples, None)
        self.assertLessEqual(37, edit.start)
        self.assertLess(edit.start, 43)
        self.assertGreaterEqual(edit.end, 49)

    def test_timing_from_chat_distinguishes_two_similar_events(self):
        samples = footage(actions=[(21, 25), (46, 50)])
        early = choose_edit(60, samples, 28)
        late = choose_edit(60, samples, 4)
        self.assertLess(early.start, 21)
        self.assertGreaterEqual(early.end, 25)
        self.assertLess(early.end, late.start)
        self.assertLess(late.start, 46)
        self.assertGreaterEqual(late.end, 50)

    def test_long_silence_and_constant_gameplay_keep_wider_context(self):
        for lead in (None, 8):
            with self.subTest(lead=lead):
                edit = choose_edit(60, footage(background=-110), lead)
                self.assertGreater(edit.end - edit.start, 22)
                self.assertLessEqual(edit.end - edit.start, 40)
                self.assertTrue(0 <= edit.start < edit.end <= 60)
                self.assertIsNone(edit.anchor)

    def test_streamer_speech_and_camera_can_drive_an_edit_over_a_static_menu(self):
        samples = footage(speech=[(22, 25), (26, 29)], camera=[(22.5, 25)])
        edit = choose_edit(60, samples, None)
        self.assertLessEqual(16, edit.start)
        self.assertLess(edit.start, 22)
        self.assertGreaterEqual(edit.end, 29)

    def test_first_frame_of_continuous_motion_is_not_a_highlight(self):
        samples = [MediaSample(i / 4, 22, 14, -18) for i in range(240)]
        samples[0] = MediaSample(0, 0, 0, -18)
        edit = choose_edit(60, samples, None)
        self.assertIsNone(edit.anchor)
        self.assertGreater(edit.start, 0)

    def test_isolated_camera_angles_during_moving_gameplay_keep_the_whole_sequence(self):
        samples = []
        for i in range(240):
            t = i / 4
            # An already-moving road alternates between light and shade early.
            motion = 15 if t >= 18 else (17 if i % 4 else 27)
            scene = 2 if t >= 18 else (4 if i % 4 else 7)
            if t in (45, 48):
                scene = 11
            samples.append(MediaSample(t, motion, 8, -22, scene))
        edit = choose_edit(60, samples, None)
        self.assertTrue(39 <= edit.start < 45, edit)
        self.assertTrue(53 <= edit.end < 59, edit)

    def test_short_source_preserves_both_edges(self):
        edit = choose_edit(8.72, [], 6)
        self.assertEqual((edit.start, edit.end), (0, 8.72))

    def test_transition_to_static_menu_preserves_the_later_spoken_moment(self):
        samples = []
        for i in range(240):
            t = i / 4
            motion, face = (16, 8) if t < 18 else (.4, .25)
            if t == 18:
                motion, face = 65, 44
            samples.append(MediaSample(t, motion, face, -13 if 36 <= t < 40 else -30,
                                       27 if t == 18 else 1))
        edit = choose_edit(60, samples, None)
        self.assertTrue(29 <= edit.start < 36, edit)
        self.assertGreater(edit.end, 40)

    def test_distinct_scene_change_at_either_edge_stays_in_the_edit(self):
        for moment in (.75, 1.0, 59.5):
            with self.subTest(moment=moment):
                samples = footage()
                samples[int(moment * 4)] = MediaSample(moment, 3, 2, -32, 85)
                edit = choose_edit(60, samples, None)
                self.assertTrue(edit.start <= moment < edit.end, edit)

    def test_chat_rise_before_available_footage_allows_a_media_only_cut(self):
        samples = footage(actions=[(47, 52)])
        edit = choose_edit(60, samples, 58)
        self.assertIsNone(edit.chat_estimate)
        self.assertLess(edit.start, 47)
        self.assertGreater(edit.end, 52)

    def test_spoken_setup_before_a_bigger_visual_payoff_is_complete(self):
        samples = footage(speech=[(24, 28)])
        samples[124] = MediaSample(31, 3, 2, -32, 60)
        edit = choose_edit(60, samples, 19)
        self.assertTrue(21 <= edit.start <= 24, edit)
        self.assertGreater(edit.end, 34)

    def test_end_uses_the_pause_before_the_next_line(self):
        samples = footage(background=-20)
        samples[120] = MediaSample(30, 3, 2, -20, 60)
        for t, db in [(37.25, -23.3), (38, -15), (38.25, -23.5)]:
            samples[int(t * 4)] = MediaSample(t, 3, 2, db)
        edit = choose_edit(60, samples, 20)
        self.assertEqual(edit.end, 37.25)
