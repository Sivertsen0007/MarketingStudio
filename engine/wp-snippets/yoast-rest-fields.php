<?php
/**
 * Marketing Studio: make Yoast's SEO title, meta description and focus keyphrase readable and writable over the WP REST API.
 *
 * Install: Code Snippets plugin ("Run everywhere") or as wp-content/mu-plugins/marketing-studio-yoast-rest.php.
 * STAGING FIRST (staging.masterliveaboards.com), then prod. See the README. Not installed by Studio.
 *
 * - Adds the three Yoast post meta keys to the `meta` object of /wp-json/wp/v2/posts.
 * - Writing needs a logged-in user who can edit posts (edit_posts) and that post (edit_post).
 * - After a REST save it asks Yoast to rebuild its indexable, so the page <head> shows the new values.
 */
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

add_action( 'init', function () {
	$keys = array( '_yoast_wpseo_title', '_yoast_wpseo_metadesc', '_yoast_wpseo_focuskw' );
	foreach ( $keys as $key ) {
		register_post_meta( 'post', $key, array(
			'type'          => 'string',
			'single'        => true,
			'show_in_rest'  => true,
			'default'       => '',
			'auth_callback' => function ( $allowed, $meta_key, $post_id ) {
				return current_user_can( 'edit_posts' ) && ( ! $post_id || current_user_can( 'edit_post', $post_id ) );
			},
			'sanitize_callback' => 'sanitize_text_field',
		) );
	}
}, 20 );

// Yoast builds its indexable on save_post, before REST writes the meta. Rebuild it once the REST request has saved everything.
add_action( 'rest_after_insert_post', function ( $post ) {
	if ( ! function_exists( 'YoastSEO' ) || ! class_exists( '\Yoast\WP\SEO\Builders\Indexable_Builder' ) ) {
		return;
	}
	try {
		$builder = YoastSEO()->classes->get( '\Yoast\WP\SEO\Builders\Indexable_Builder' );
		if ( $builder && method_exists( $builder, 'build_for_id_and_type' ) ) {
			$builder->build_for_id_and_type( $post->ID, 'post' );
		}
	} catch ( \Throwable $e ) {
		error_log( 'Marketing Studio Yoast rebuild: ' . $e->getMessage() );
	}
}, 20 );
